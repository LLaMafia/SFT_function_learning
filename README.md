# SFT: what is it really learning?

This repo is forked from the [DPO repo](https://github.com/eric-mitchell/direct-preference-optimization).
However, this repo is **NOT** to explore alignment of LLMs, but rather to explore what the LLMs are learning during SFT.

## Motivation

It has been long argued that SFT is enough for alignment, i.e. LLMs can generate **"PROPER RESPONSES"** after SFT.
Yet, in my own previous experiment, I observed that the likelihood of the "negative responses" (or "improper responses") also keeps increasing during SFT, even though the learning target is only the positive/preferred responses.
So, this leads to the question of this repo: **what is the LLM really learning during SFT?**

More specifically, I wonder if the LLM is learning to generate the "proper responses" or just "responses".
To answer this question, I construct the synthetic data followed the method introduced in the next section, and then train the LLMs on only the "proper responses" with SFT.
The likelihood of various kinds of responses over SFT is then recoded and showed at the end of this README.

## Synthetic Data

To answer the question above, beyond the "proper/chosen responses" to the prompts inthe `HH/helpful-base` dataset from Anthropic, I also generate the following kinds of responses the same prompts:

1. **Rejected**: the responses that are rejected by the user, compared to the chosen responses.

2. **Paraphrase**: the responses that are paraphrases of the *REJECTED* responses, done by Gemini-Pro.

3. **Vairant**: the responses from Gemini-Pro.

4. **Random**: the responses to randomly selected prompts.

5. **Non-response**: sentences that are randomly generated from Gemini-Pro, thus not even considered as responses.

(Why paraphrases of the rejected responses?)

## Results

