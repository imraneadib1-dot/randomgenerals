"""Agents that run inside the site, on every reply.

"Agent" here means a small piece of code that observes what the models
do, decides something, and acts on the next request - not a chatbot
with a name. Three of them, each answering one question:

  router      WHO SHOULD ANSWER? Every reply leaves a row in
              channel_stats: which provider and model, how long the
              first token took, how long the whole thing took, whether
              it worked. The router reads the last few minutes of that
              and demotes a channel that is failing or crawling right
              now, before the next person waits on it. This is how the
              site gets quicker without anyone changing a config: the
              routing table says what to prefer, the router says what
              is actually answering today.

  verifier    IS THE CODE RIGHT? A reply in the code bay has its
              Python run in the sandbox the moment it is finished. A
              block that raises is handed back to the model with the
              traceback for one repair, and the reader sees both - the
              failure and the fix - under a "Self-check" heading. A
              wrong answer that says it checked itself is worse than a
              wrong answer, so a repair that also fails says that.

  evaluate    IS IT GETTING BETTER? The offline harness, run by hand:
              a file of graded questions (evals/cases.jsonl), every
              configured channel asked each one, accuracy and latency
              per model written to evals/results/. This is what
              "training" honestly means for an app built on hosted
              models it cannot fine-tune: measure, then move the routing
              table and the prompts toward what measures best. The
              thumbs people leave on replies (reply_feedback) feed it -
              a disliked answer becomes a candidate case.

None of them imports app.py. The router is given records and asked for
rankings; the verifier is given text and a way to ask the model; the
evaluator is a command. app.py wires them in.
"""
