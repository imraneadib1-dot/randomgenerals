# pip

I started building pip to help students around the world get an AI they can use
for school, code, reading, 3D modeling, image generation and video generation,
without having to pay for it.

**Try it here: [randomgenerals.com](https://randomgenerals.com)**. It's free.

## About me

I'm 16 and I've been coding for about 3 years. I've used C++, C, Python, Rust,
Java, JavaScript, HTML/CSS and Docker. I work on pip on Friday nights and weekends.

## How it started

The first version was a mess. It used Ollama and had an ugly design I didn't like.
I made it better with better prompts, but mostly with better code.

## Why I fine-tuned my own model

I wanted pip to be my own original AI, not just another app on top of someone
else's API. pip 1.0 is Qwen3.5-4B with a LoRA adapter I trained myself.

The name pip I found randomly on the internet. Barely anyone uses it as a model
name, so I took it.

## The models

Besides pip 1.0, pip also uses gpt-oss-120b and Gemma 31B. Pro uses a Claude model.
I emailed sponsors to help with the cost but got ignored, so I pay for it with
my own money.

## The hardest bug

Models would sometimes just stop responding. With gpt-oss-120b it was the token
limit. I had to rewrite and debug it a lot before I found a token limiter that
lets it finish heavy tasks without stopping. It still has some problems, but now
it can build a whole website.

## Free vs Pro

pip is free. Pro is only for really heavy tasks, and I made it cheap on purpose
($3.99) so students can afford it, unlike other AIs that cost $20+ a month.

## Who uses it

My parents, my family, my friends, and some people I don't even know.

## What's next

Inshallah, better models, and a workspace where you can do anything in one place.

I also want a 3D printer to print a case and parts for my Arduino project: a small
AI assistant device you can talk to about anything without needing to sit at your
computer. You talk to the device and it does all the work on your computer for you.

## Running it yourself

```bash
pip install -r requirements.txt
python pip_server.py   # runs the model
python app.py          # runs the website
```
