// Compiled training core for CharRNN - the same math as rnn.py's forward()
// and backward(), written by hand in C++ instead of expressed as NumPy
// array ops. Exists for one reason: rnn.py's training loop crosses the
// Python/NumPy boundary roughly a dozen times per training step (one call
// per matrix op), and every crossing costs a few microseconds of dispatch
// overhead that pure compute doesn't need to pay. Fusing the whole step -
// embedding lookup, the recurrent loop, backprop through time, gradient
// clipping - into one function call removes all of that at once.
//
// This is NOT expected to out-multiply OpenBLAS at the actual matrix
// multiplies - NumPy already hands those to a hand-tuned SIMD BLAS kernel,
// and the loops below do not try to out-engineer that. What this buys is
// fewer round trips and better cache reuse of the hidden state across the
// recurrence, which array-at-a-time NumPy code structurally can't do since
// it materializes every intermediate to memory. It is compiled with
// -O3 -march=native so the compiler auto-vectorizes what it can, and with
// OpenMP on the loops that are genuinely independent across the batch.
//
// Every array here is a flat, row-major (C-contiguous) float32 buffer -
// the same layout numpy uses by default - so the Python side can hand over
// `arr.ctypes.data` directly with no copying or repacking.
#include <cmath>
#include <cstring>
#include <cstdint>
#include <algorithm>
#include <vector>

#if defined(_WIN32)
  #define API extern "C" __declspec(dllexport)
#else
  #define API extern "C" __attribute__((visibility("default")))
#endif

namespace {

// out[b, :] = A[b, :] @ M   for every b in [0,B), where A is (B,K), M is
// (K,N), out is (B,N). The one matmul shape every step of this file needs -
// batch of row-vectors against a shared matrix - so it's written once and
// reused rather than inlined at each call site.
void batch_matmul(const float* A, const float* M, float* out,
                  int B, int K, int N, bool accumulate) {
  #pragma omp parallel for
  for (int b = 0; b < B; b++) {
    const float* a = A + (size_t)b * K;
    float* o = out + (size_t)b * N;
    if (!accumulate) std::fill(o, o + N, 0.0f);
    for (int k = 0; k < K; k++) {
      float av = a[k];
      const float* mrow = M + (size_t)k * N;
      for (int n = 0; n < N; n++) o[n] += av * mrow[n];
    }
  }
}

// out (K,N) += A^T @ Dout, where A is (B,K), Dout is (B,N). The gradient
// counterpart of batch_matmul above - every weight matrix's gradient is a
// sum of outer products over the batch, which is exactly this shape.
// Each output row k is written by every b independently (a fixed b
// contributes to all k), so parallelizing over k keeps every thread
// writing disjoint memory with no locking needed.
void outer_accum(const float* A, const float* Dout, float* out,
                 int B, int K, int N) {
  #pragma omp parallel for
  for (int k = 0; k < K; k++) {
    float* orow = out + (size_t)k * N;
    for (int b = 0; b < B; b++) {
      float av = A[(size_t)b * K + k];
      const float* drow = Dout + (size_t)b * N;
      for (int n = 0; n < N; n++) orow[n] += av * drow[n];
    }
  }
}

void softmax_rows(float* logits, int rows, int V) {
  #pragma omp parallel for
  for (int r = 0; r < rows; r++) {
    float* row = logits + (size_t)r * V;
    float mx = row[0];
    for (int v = 1; v < V; v++) mx = std::max(mx, row[v]);
    float sum = 0.0f;
    for (int v = 0; v < V; v++) { row[v] = std::exp(row[v] - mx); sum += row[v]; }
    float inv = 1.0f / sum;
    for (int v = 0; v < V; v++) row[v] *= inv;
  }
}

}  // namespace

// One fused forward + backward pass, matching rnn.py's CharRNN.forward()
// and CharRNN.backward() exactly (same equations, same clipping rule).
//
//   x, y        : (B, T) int32 character ids - input window, target window
//   C           : (Vsize, E)   character embedding table
//   Wxh         : (E, H)
//   Whh         : (H, H)
//   bh          : (H,)
//   Why         : (H, Vsize)
//   by          : (Vsize,)
//   dC/dWxh/dWhh/dbh/dWhy/dby : same shapes as the params above - written
//                               with this step's gradients (not accumulated
//                               into whatever was there before)
//   out_loss    : mean cross-entropy over all B*T predictions
//   clip        : global-norm gradient clip threshold, 0 to disable
API void rnn_train_step(
    const int32_t* x, const int32_t* y,
    const float* C, const float* Wxh, const float* Whh,
    const float* bh, const float* Why, const float* by,
    int B, int T, int E, int H, int Vsize,
    float* dC, float* dWxh, float* dWhh, float* dbh,
    float* dWhy, float* dby, float* out_loss, float clip) {

  // Backward needs each weight matrix transposed (dh_next = da @ Whh^T,
  // and similarly for Why and Wxh). Transposing once here costs O(H^2) -
  // trivial next to the O(T*B*H^2) main loops - and turns every backward
  // access from a stride-H column read into a contiguous row read, which
  // is the difference between the compiler vectorizing the inner loop and
  // not.
  std::vector<float> WhhT((size_t)H * H);
  for (int r = 0; r < H; r++)
    for (int c = 0; c < H; c++) WhhT[(size_t)r * H + c] = Whh[(size_t)c * H + r];

  std::vector<float> WhyT((size_t)Vsize * H);
  for (int r = 0; r < Vsize; r++)
    for (int c = 0; c < H; c++) WhyT[(size_t)r * H + c] = Why[(size_t)c * Vsize + r];

  std::vector<float> WxhT((size_t)H * E);
  for (int r = 0; r < H; r++)
    for (int c = 0; c < E; c++) WxhT[(size_t)r * E + c] = Wxh[(size_t)c * H + r];

  // ---- forward ----
  // emb is (B,T,E) to match how x is laid out; xh/hs are time-major
  // (T,B,H) so hs[t] is one contiguous block - the same layout choice
  // rnn.py's NumPy version makes, and for the same reason: every step of
  // the recurrence touches all of hs[t], so it needs to be one contiguous
  // read instead of a strided one.
  std::vector<float> emb((size_t)B * T * E);
  #pragma omp parallel for
  for (int b = 0; b < B; b++)
    for (int t = 0; t < T; t++) {
      int32_t ch = x[(size_t)b * T + t];
      std::memcpy(&emb[((size_t)b * T + t) * E], &C[(size_t)ch * E],
                 E * sizeof(float));
    }

  // xh[t,b,:] = emb[b,t,:] @ Wxh + bh  -- the input side of every step,
  // computed once for all T before the sequential loop starts.
  std::vector<float> xh((size_t)T * B * H);
  #pragma omp parallel for collapse(2)
  for (int t = 0; t < T; t++)
    for (int b = 0; b < B; b++) {
      const float* e = &emb[((size_t)b * T + t) * E];
      float* o = &xh[((size_t)t * B + b) * H];
      std::memcpy(o, bh, H * sizeof(float));
      for (int k = 0; k < E; k++) {
        float ev = e[k];
        const float* wrow = &Wxh[(size_t)k * H];
        for (int h = 0; h < H; h++) o[h] += ev * wrow[h];
      }
    }

  std::vector<float> hs((size_t)(T + 1) * B * H, 0.0f);
  std::vector<float> hprev_whh((size_t)B * H);
  for (int t = 0; t < T; t++) {
    const float* h_t = &hs[(size_t)t * B * H];
    batch_matmul(h_t, Whh, hprev_whh.data(), B, H, H, /*accumulate=*/false);
    float* h_next = &hs[(size_t)(t + 1) * B * H];
    const float* xh_t = &xh[(size_t)t * B * H];
    for (size_t i = 0; i < (size_t)B * H; i++)
      h_next[i] = std::tanh(xh_t[i] + hprev_whh[i]);
  }

  // logits[t,b,:] = hs[t+1,b,:] @ Why + by
  std::vector<float> logits((size_t)T * B * Vsize);
  #pragma omp parallel for
  for (int i = 0; i < T * B; i++) {
    const float* h = &hs[(size_t)(i + B) * H];  // hs[1:] flattened, offset by one step
    float* o = &logits[(size_t)i * Vsize];
    std::memcpy(o, by, Vsize * sizeof(float));
    for (int k = 0; k < H; k++) {
      float hv = h[k];
      const float* wrow = &Why[(size_t)k * Vsize];
      for (int v = 0; v < Vsize; v++) o[v] += hv * wrow[v];
    }
  }

  // ---- loss + dlogits ----
  // y is (B,T); logits/dlogits are (T,B,V), so the target for row (t,b) is
  // y[b,t], not y[t,b] - the same transpose rnn.py's y_tm does explicitly.
  std::vector<float> dlogits((size_t)T * B * Vsize);
  std::memcpy(dlogits.data(), logits.data(), dlogits.size() * sizeof(float));
  softmax_rows(dlogits.data(), T * B, Vsize);

  double loss_sum = 0.0;
  float scale = 1.0f / (float)(B * T);
  for (int t = 0; t < T; t++)
    for (int b = 0; b < B; b++) {
      int row = t * B + b;
      int32_t target = y[(size_t)b * T + t];
      float p = dlogits[(size_t)row * Vsize + target];
      loss_sum += -std::log(std::max(p, 1e-12f));
      dlogits[(size_t)row * Vsize + target] -= 1.0f;
    }
  for (auto& v : dlogits) v *= scale;
  *out_loss = (float)(loss_sum / (B * T));

  // ---- backward ----
  std::memset(dWhy, 0, (size_t)H * Vsize * sizeof(float));
  outer_accum(&hs[(size_t)B * H], dlogits.data(), dWhy, T * B, H, Vsize);
  std::memset(dby, 0, Vsize * sizeof(float));
  for (int i = 0; i < T * B; i++) {
    const float* row = &dlogits[(size_t)i * Vsize];
    for (int v = 0; v < Vsize; v++) dby[v] += row[v];
  }

  // dh_out[t,b,:] = dlogits[t,b,:] @ Why^T  - every step's gradient from
  // its own prediction, independent of the recurrence, so done for all T
  // at once rather than inside the sequential loop below.
  std::vector<float> dh_out((size_t)T * B * H);
  #pragma omp parallel for
  for (int i = 0; i < T * B; i++) {
    const float* d = &dlogits[(size_t)i * Vsize];
    float* o = &dh_out[(size_t)i * H];
    std::fill(o, o + H, 0.0f);
    for (int v = 0; v < Vsize; v++) {
      float dv = d[v];
      const float* wrow = &WhyT[(size_t)v * H];
      for (int h = 0; h < H; h++) o[h] += dv * wrow[h];
    }
  }

  // The one genuinely sequential part: walk backwards through time,
  // carrying the blame each hidden state inherits from the step it fed
  // forward into. da_all[t] is that blame after the tanh derivative.
  std::vector<float> da_all((size_t)T * B * H);
  std::vector<float> dh_next((size_t)B * H, 0.0f);
  for (int t = T - 1; t >= 0; t--) {
    const float* h_t1 = &hs[(size_t)(t + 1) * B * H];
    const float* dho = &dh_out[(size_t)t * B * H];
    float* da = &da_all[(size_t)t * B * H];
    for (size_t i = 0; i < (size_t)B * H; i++) {
      float dh = dho[i] + dh_next[i];
      da[i] = dh * (1.0f - h_t1[i] * h_t1[i]);
    }
    // hand back one more step: dh_next = da @ Whh^T
    batch_matmul(da, WhhT.data(), dh_next.data(), B, H, H, /*accumulate=*/false);
  }

  std::memset(dbh, 0, H * sizeof(float));
  for (int i = 0; i < T * B; i++) {
    const float* row = &da_all[(size_t)i * H];
    for (int h = 0; h < H; h++) dbh[h] += row[h];
  }

  std::memset(dWhh, 0, (size_t)H * H * sizeof(float));
  outer_accum(hs.data(), da_all.data(), dWhh, T * B, H, H);  // hs[:-1] flattened == hs.data() for T*B rows

  // emb is (B,T,E); da_all is (T,B,H). dWxh needs them aligned the same
  // way, so build a time-major copy of emb once - same reasoning as the
  // WhhT/WhyT/WxhT transposes above, trading a small upfront copy for a
  // contiguous accumulation instead of a strided one.
  std::vector<float> emb_tm((size_t)T * B * E);
  #pragma omp parallel for collapse(2)
  for (int t = 0; t < T; t++)
    for (int b = 0; b < B; b++)
      std::memcpy(&emb_tm[((size_t)t * B + b) * E],
                 &emb[((size_t)b * T + t) * E], E * sizeof(float));

  std::memset(dWxh, 0, (size_t)E * H * sizeof(float));
  outer_accum(emb_tm.data(), da_all.data(), dWxh, T * B, E, H);

  std::memset(dC, 0, (size_t)Vsize * E * sizeof(float));
  // demb[t,b,:] = da_all[t,b,:] @ Wxh^T, then scattered into dC by
  // character id. Not parallelized over t/b: several positions can share
  // a character, so every write has to land in the same accumulator with
  // no two threads racing on the same row.
  for (int t = 0; t < T; t++)
    for (int b = 0; b < B; b++) {
      const float* da = &da_all[((size_t)t * B + b) * H];
      int32_t ch = x[(size_t)b * T + t];
      float* orow = &dC[(size_t)ch * E];
      for (int k = 0; k < H; k++) {
        float dv = da[k];
        const float* wrow = &WxhT[(size_t)k * E];
        for (int e = 0; e < E; e++) orow[e] += dv * wrow[e];
      }
    }

  // ---- global-norm gradient clipping ----
  if (clip > 0.0f) {
    auto sumsq = [](const float* p, size_t n) {
      double s = 0.0;
      for (size_t i = 0; i < n; i++) s += (double)p[i] * (double)p[i];
      return s;
    };
    double total = sumsq(dC, (size_t)Vsize * E) + sumsq(dWxh, (size_t)E * H) +
                   sumsq(dWhh, (size_t)H * H) + sumsq(dbh, H) +
                   sumsq(dWhy, (size_t)H * Vsize) + sumsq(dby, Vsize);
    double norm = std::sqrt(total);
    if (norm > clip) {
      float scale_c = (float)(clip / norm);
      auto apply = [scale_c](float* p, size_t n) {
        for (size_t i = 0; i < n; i++) p[i] *= scale_c;
      };
      apply(dC, (size_t)Vsize * E); apply(dWxh, (size_t)E * H);
      apply(dWhh, (size_t)H * H); apply(dbh, H);
      apply(dWhy, (size_t)H * Vsize); apply(dby, Vsize);
    }
  }
}
