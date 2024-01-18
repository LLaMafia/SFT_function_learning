# SFT: what is it really learning?

Author: Shangmin Guo
Contact: [s.guo@ed.ac.uk](mailto:s.guo@ed.ac.uk)

This repo is forked from the [DPO repo](https://github.com/eric-mitchell/direct-preference-optimization).
However, this repo is **NOT** to explore alignment of LLMs, but rather to explore what the LLMs are learning during SFT.

## Motivation

It has been long argued that SFT is enough for alignment, i.e. LLMs can generate **"PROPER RESPONSES"** after SFT.
Yet, in my own previous experiment, I observed that the likelihood of the "negative responses" (or "improper responses") also keeps increasing during SFT, even though the learning target is ONLY the positive/preferred responses.
So, this leads to the question of this repo: **what is the LLM really learning during SFT?**

More specifically, I wonder if the LLM is learning to generate the "proper responses" or just "responses".
To answer this question, I construct the synthetic data followed the method introduced in the next section, and then train the LLMs on only the "proper responses" with SFT.
The likelihood of various kinds of responses over SFT is then recoded and showed at the end of this README.

More broadly, the key hypothesis can be generalised as: 
> **Hypothesis**: given a set of samples $\mathbb{D}=\{(x_i,y_i)\}\_{i=1}^N$ generated by a target function $f_{tgt}$, if we train a model on $\mathbb{D}$, the models is not neccessarily learning $f_{tgt}$, but rather learning a simpler function $f_{smp}$ that can generate $\mathbb{D}$.

The above hypothesis says that the dataset $\mathbb{D}$ can represent a functoin family $\mathcal{F}$, not necessarily only a single function, and the model is learning the simplest function for it, i.e. $f_{smp}\in\mathcal{F}$, not neccessarily the $f_{tgt}$ used to generate the data.
For example, in alignment, the dataset $\mathbb{D}$ can represent the function family of "all kinds of responses", and the target function $f_{tgt}$ is the "proper responses" function.

So far, the unclear and interesting part is that how can we tell the exact function that the model is learning through SFT on $\mathbb{D}$.
If we say it's the "simplest function", then how can we define "simplicity"?
Furthermore, given a target function $f_{tgt}$, how can we construct a dataset $\mathbb{D}$ that can represent only $f_{tgt}$ (i.e. $\mathcal{F}=\{f_{tgt}\}$)?
For example, if $f_{tgt}$ is "proper response" and we can construct such a dataset $\mathbb{D}$, then SFT might indeed become sufficient for aligning LLMs, thus the alignment step is not necessary anymore.

## Synthetic Data

To answer the question above, beyond the "proper/chosen responses" to the prompts inthe `HH/helpful-base` dataset from Anthropic, I also need to synthesise more data on it.
Following the function hypothesis above, I hereby consider from the perspective that $(x,y)$ in the dataset $\mathbb{D}$ represent a family of functions $\mathcal{F}: \mathcal{X} \mapsto \mathcal{Y}$, where $\mathcal{X}$ is the set of all possible prompts and $\mathcal{Y}$ is the set of all possible responses.

For a given prompt $x_i$, the positive is $y_i^+$ which represents a "proper response" to $x_i$, and the tuple $(x_i,y_i^+)$ says that $x_i$ should be mapped to $y_i^+$, i.e. $x_i \mapsto y_i^+$.
I then generate the following kinds of responses the same prompt $x_i$:

1. **Rejected response $y_i^-$**: a response that is sampled from $f_{tgt}(x_i)$ as well, but is less preferred by the user, compared to $y_i^+$.

2. **Paraphrase of rejected response $\tilde{y}_i^-$**: a paraphrase of the *REJECTED* response $y_i^-$, done by Gemini-Pro.
$\tilde{y}\_i^-$ should be in $\mathcal{F}$, but is *further away from the distribution* specified by $f_{tgt}(x_i)$ than $y_i^+$ and $y_i^-$.

3. **Vairant response $y_i'$**: a responses generated by Gemini-Pro to $x_i$, thus $y_i'in\mathcal{F}$ but should be *out of the distribution* specified by $f_{tgt}(x_i)$.

4. **Random response $y_j^+$**: the preferered response to a randomly selected prompt $x_j$.
N.B. the index is $j$ here instead of $i$, thus $y_j^+\in\mathcal{F}$ but is mapped from a different $x_j$.
$y_j^+$ should be totally out of the distribution specified by $f_{tgt}(x_i)$.

5. **Non-response $\bar{y}_i$**: a random sentence generated by Gemini-Pro, thus not a response, i.e. $\bar{y}_i\notin\mathcal{F}$.

## Results

Now, let's see how the likelihood of the above kinds of responses change over SFT.
N.B. the learning target is **ONLY** the "proper responses" $y_i^+$!

![Log-probs of various responses on EVALUATION set.](https://github.com/Shawn-Guo-CN/SFT_function_learning/blob/main/results/logp_eval.png)


As can be seen from the above figure, the log-probabilities of various kinds of responses change in different ways over SFT.
Briefly speaking, all of them increase over SFT, except for the "non-response" $\bar{y}_i$.


## Conclusion

The results above suggest that SFT is actually learning to generate "responses" in general, not even only the "responses" to $x_i$, since $y_j^+$ also increases.
Not surprisingly, log-likelihood of different kinds of responses change in different degrees, which suggests that the LLMs is actually fitting the distribution which generates the data.

More interestingly, the log-probability of "rejected responses" $y_i^-$ is alway higher than that of the "proper responses" $y_i^+$, which suggests that the LLMs is actually learning to generate the "rejected responses" instead of the "proper responses".
(PS: the second half part after the comma of this conclusion is done by `GitHub-Copilot`)

Overall, the results signifies that SFT is not enough for alignment, and the alignment step is still necessary.

## TODO

- [ ] Discuss the degrees of the changes of log-probabilities of various responses.
- [ ] Figure out how to define the "simplicity" of a function for a given model.
- [ ] Figure out how to construct a dataset $\mathbb{D}$ that can represent only $f_{tgt}$, i.e. $\mathcal{F}=\{f_{tgt}\}$.
