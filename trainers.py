import torch
torch.backends.cuda.matmul.allow_tf32 = True
import torch.nn.functional as F
import torch.nn as nn
import transformers
from omegaconf import DictConfig

import torch.distributed as dist
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
    StateDictType,
    BackwardPrefetch,
    ShardingStrategy,
    CPUOffload,
)
from torch.distributed.fsdp.api import FullStateDictConfig
from torch.distributed.fsdp.api import FullOptimStateDictConfig
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy
import tensor_parallel as tp
import contextlib

from preference_datasets import get_batch_iterator
from utils import (
    slice_and_move_batch_for_device,
    formatted_dict,
    all_gather_if_needed,
    pad_to_length,
    get_block_class_from_model,
    rank0_print,
    get_local_dir,
)
import numpy as np
import wandb
import tqdm

import random
import os
from collections import defaultdict
import time
import json
import functools
from typing import Optional, Dict, List, Union, Tuple


def _get_batch_logps(
        logits: torch.FloatTensor,
        labels: torch.LongTensor,
        average_log_prob: bool = False
) -> torch.FloatTensor:
    """Compute the log probabilities of the given labels under the given logits.

    Args:
        logits: Logits of the model (unnormalized). Shape: (batch_size,
                sequence_length, vocab_size)
        labels: Labels for which to compute the log probabilities. Label tokens
                with a value of -100 are ignored.
                Shape: (batch_size, sequence_length)
        average_log_prob: If True, return the average log probability per 
                          (non-masked) token. Otherwise, return the sum of the 
                          log probabilities of the (non-masked) tokens.

    Returns:
        A tensor of shape (batch_size,) containing the average/sum log
        probabilities of the given labels under the given logits.
    """
    assert logits.shape[:-1] == labels.shape

    labels = labels[:, 1:].clone()
    logits = logits[:, :-1, :]
    loss_mask = (labels != -100)

    # dummy token; we'll ignore the losses on these tokens later
    labels[labels == -100] = 0

    per_token_logps = torch.gather(
        logits.log_softmax(-1), dim=2, index=labels.unsqueeze(2)
    ).squeeze(2)

    if average_log_prob:
        return (per_token_logps * loss_mask).sum(-1) / loss_mask.sum(-1)
    else:
        return (per_token_logps * loss_mask).sum(-1)


class BasicTrainer(object):
    def __init__(
            self, policy: nn.Module, config: DictConfig, seed: int,
            run_dir: str, reference_model: Optional[nn.Module] = None,
            rank: int = 0, world_size: int = 1
    ) -> None:
        """A trainer for a language model, supporting either SFT training.

        If multiple GPUs are present, naively splits the model across them, 
        effectively offering N times available memory, but without any parallel 
        computation.
        """
        self.seed = seed
        self.rank = rank
        self.world_size = world_size
        self.config = config
        self.run_dir = run_dir

        tokenizer_name_or_path = \
            config.model.tokenizer_name_or_path or config.model.name_or_path
        rank0_print(f'Loading tokenizer {tokenizer_name_or_path}')
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            tokenizer_name_or_path, cache_dir=get_local_dir(config.local_dirs)
        )
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

        data_iterator_kwargs = dict(
            names=config.datasets,
            tokenizer=self.tokenizer,
            shuffle=True,
            max_length=config.max_length,
            max_prompt_length=config.max_prompt_length,
        )

        self.policy = policy
        self.reference_model = reference_model

        self.train_iterator = get_batch_iterator(
            **data_iterator_kwargs,
            split='train',
            n_epochs=config.n_epochs,
            n_examples=config.n_examples,
            batch_size=config.batch_size,
            silent=rank != 0,
        )
        rank0_print(f'Loaded train data iterator')
        self.eval_iterator = get_batch_iterator(
            **data_iterator_kwargs,
            split='test',
            n_examples=config.n_eval_examples,
            batch_size=config.eval_batch_size,
            silent=rank != 0,
        )
        self.eval_batches = list(self.eval_iterator)
        rank0_print(
            f'Loaded {len(self.eval_batches)} eval batches ' + \
            f'of size {config.eval_batch_size}'
        )

    def get_batch_samples(
            self, batch: Dict[str, torch.LongTensor]
    ) -> Tuple[str, str]:
        """Generate samples from the policy for the given batch of inputs."""

        # FSDP generation according to
        # https://github.com/pytorch/pytorch/issues/100069
        ctx = lambda: (
            FSDP.summon_full_params(
                self.policy, writeback=False, recurse=False
            ) if 'FSDP' in self.config.trainer else contextlib.nullcontext()
        )
        with ctx():
            policy_output = self.policy.generate(
                batch['prompt_input_ids'],
                attention_mask=batch['prompt_attention_mask'],
                max_length=self.config.max_length,
                do_sample=True,
                pad_token_id=self.tokenizer.pad_token_id
            )

        if self.config.loss.name in {'dpo', 'ipo'}:
           raise NotImplementedError('DPO/IPO are not implemented')

        policy_output = pad_to_length(
            policy_output, self.config.max_length, self.tokenizer.pad_token_id
        )
        policy_output = all_gather_if_needed(
            policy_output, self.rank, self.world_size
        )
        policy_output_decoded = self.tokenizer.batch_decode(
            policy_output, skip_special_tokens=True
        )

        if self.config.loss.name in {'dpo', 'ipo'}:
            raise NotImplementedError('DPO/IPO are not implemented')
        reference_output_decoded = []

        return policy_output_decoded, reference_output_decoded

    def get_batch_metrics(
        self,
        batch: Dict[str, Union[List, torch.LongTensor]],
        loss_config: DictConfig,
        train=True
    ) -> Tuple[torch.FloatTensor, Dict[str, List]]:
        """Compute the SFT loss and other metrics for the given batch of inputs.
        """

        metrics = {}
        train_test = 'train' if train else 'eval'

        if loss_config.name != 'sft':
            raise NotImplementedError(
                f'loss {loss_config.name} not implemented'
            )

        policy_chosen_logits = self.policy(
            input_ids=batch['chosen_input_ids'],
            attention_mask=batch['chosen_attention_mask']
        ).logits.to(torch.float32)
        policy_chosen_logps = _get_batch_logps(
            policy_chosen_logits, batch['chosen_labels'],
            average_log_prob=False
        )
        losses = -policy_chosen_logps
        
        with torch.no_grad():
            for k in [
                'rejected', 'random', 'paraphrase', 'variant', 'nonresponse'
            ]:
                policy_predict_logtis = self.policy(
                    input_ids=batch[f'{k}_input_ids'],
                    attention_mask=batch[f'{k}_attention_mask']
                ).logits.detach().to(torch.float32)
                policy_predict_logps = _get_batch_logps(
                    policy_predict_logtis, batch[f'{k}_labels'],
                    average_log_prob=False
                )
                del policy_predict_logtis
                metrics[f'logps_{train_test}/{k}'] = \
                    policy_predict_logps.cpu().numpy().tolist()

        policy_chosen_logps = all_gather_if_needed(
            policy_chosen_logps.detach(), self.rank, self.world_size
        )
        metrics[f'logps_{train_test}/chosen'] = \
            policy_chosen_logps.cpu().numpy().tolist()

        all_devices_losses = all_gather_if_needed(
            losses.detach(), self.rank, self.world_size
        )
        metrics[f'loss/{train_test}'] = \
            all_devices_losses.cpu().numpy().tolist()

        return losses.mean(), metrics

    def train(self):
        """Begin either SFT or DPO training, with periodic evaluation."""

        rank0_print(f'Using {self.config.optimizer} optimizer')
        self.optimizer = getattr(torch.optim, self.config.optimizer)(
            self.policy.parameters(), lr=self.config.lr
        )
        self.scheduler = torch.optim.lr_scheduler.LambdaLR(
            self.optimizer,
            lr_lambda=lambda step: min(
                1.0, (step + 1) / (self.config.warmup_steps + 1)
            )
        )

        torch.manual_seed(self.seed)
        np.random.seed(self.seed)
        random.seed(self.seed)

        if self.config.loss.name in {'dpo', 'ipo'}:
            self.reference_model.eval()

        self.example_counter = 0
        self.batch_counter = 0
        last_log = None

        for batch in self.train_iterator:
            #### BEGIN EVALUATION ####
            if self.example_counter % self.config.eval_every == 0 and \
                (self.example_counter > 0 or self.config.do_first_eval):
                rank0_print(
                    f'Running evaluation after {self.example_counter} ' + \
                    f'train examples'
                )
                self.policy.eval()

                all_eval_metrics = defaultdict(list)
                if self.config.sample_during_eval:
                    all_policy_samples, all_reference_samples = [], []
                    policy_text_table = wandb.Table(
                        columns=["step", "prompt", "sample"]
                    )
                    if self.config.loss.name in {'dpo', 'ipo'}:
                        raise NotImplementedError('DPO/IPO are not implemented')

                for eval_batch in (
                    tqdm.tqdm(self.eval_batches, desc='Computing eval metrics')\
                    if self.rank == 0 else self.eval_batches
                ):
                    local_eval_batch = slice_and_move_batch_for_device(
                        eval_batch, self.rank, self.world_size, self.rank
                    )
                    with torch.no_grad():
                        _, eval_metrics = self.get_batch_metrics(
                            local_eval_batch, self.config.loss, train=False
                        )

                    for k, v in eval_metrics.items():
                        all_eval_metrics[k].extend(v)

                if self.config.sample_during_eval:
                    if self.config.n_eval_model_samples < \
                        self.config.eval_batch_size:
                        rank0_print(
                            f'Warning: n_eval_model_samples ' + \
                            f'({self.config.n_eval_model_samples}) < ' + \
                            f'eval_batch_size ({self.config.eval_batch_size}).'\
                            + f'Sampling from the first complete eval ' + \
                            'batch of prompts.'
                        )
                        sample_batches = self.eval_batches[:1]
                    else:
                        n_sample_batches = self.config.n_eval_model_samples //\
                        self.config.eval_batch_size
                        sample_batches = self.eval_batches[:n_sample_batches]
                    for eval_batch in (
                        tqdm.tqdm(sample_batches, desc='Generating samples...')\
                        if self.rank == 0 else sample_batches
                    ):
                        local_eval_batch = slice_and_move_batch_for_device(
                            eval_batch, self.rank, self.world_size, self.rank
                        )
                        policy_samples, reference_samples = \
                            self.get_batch_samples(local_eval_batch)

                        all_policy_samples.extend(policy_samples)
                        all_reference_samples.extend(reference_samples)

                        for prompt, sample in zip(
                            eval_batch['prompt'], policy_samples
                        ):
                            policy_text_table.add_data(
                                self.example_counter, prompt, sample
                            )
                        if self.config.loss.name in {'dpo', 'ipo'}:
                            raise NotImplementedError(
                                'DPO/IPO are not implemented'
                            )

                mean_eval_metrics = {
                    k: sum(v) / len(v) for k, v in all_eval_metrics.items()
                }
                rank0_print(
                    f'eval after {self.example_counter}:' + \
                    f'{formatted_dict(mean_eval_metrics)}'
                )
                if self.config.sample_during_eval:
                    rank0_print(json.dumps(all_policy_samples[:10], indent=2))
                    if self.config.loss.name in {'dpo', 'ipo'}:
                        raise NotImplementedError(
                            'DPO/IPO are not implemented'
                        )

                if self.config.wandb.enabled and self.rank == 0:
                    wandb.log(mean_eval_metrics, step=self.example_counter)

                    if self.config.sample_during_eval:
                        wandb.log(
                            {"policy_samples": policy_text_table},
                            step=self.example_counter
                        )
                        if self.config.loss.name in {'dpo', 'ipo'}:
                            raise NotImplementedError(
                                'DPO/IPO are not implemented'
                            )

                if self.example_counter > 0 and self.example_counter and \
                    self.example_counter % (20 * self.config.eval_every == 0):
                    if self.config.debug:
                        rank0_print('skipping save in debug mode')
                    else:
                        output_dir = os.path.join(
                            self.run_dir, f'step-{self.example_counter}'
                        )
                        rank0_print(
                            f'creating checkpoint to write to {output_dir}...'
                        )
                        self.save(output_dir, mean_eval_metrics)
            #### END EVALUATION ####

            #### BEGIN TRAINING ####
            self.policy.train()

            start_time = time.time()
            batch_metrics = defaultdict(list)
            for microbatch_idx in range(
                self.config.gradient_accumulation_steps
            ):
                global_microbatch = slice_and_move_batch_for_device(
                    batch, microbatch_idx, self.config.gradient_accumulation_steps, self.rank
                )
                local_microbatch = slice_and_move_batch_for_device(
                    global_microbatch, self.rank, self.world_size, self.rank
                )
                loss, metrics = self.get_batch_metrics(
                    local_microbatch, self.config.loss, train=True
                )
                (loss / self.config.gradient_accumulation_steps).backward()

                for k, v in metrics.items():
                    batch_metrics[k].extend(v)

            grad_norm = self.clip_gradient()
            self.optimizer.step()
            self.scheduler.step()
            self.optimizer.zero_grad()

            step_time = time.time() - start_time
            examples_per_second = self.config.batch_size / step_time
            batch_metrics['examples_per_second'].append(examples_per_second)
            batch_metrics['grad_norm'].append(grad_norm)

            self.batch_counter += 1
            self.example_counter += self.config.batch_size

            if last_log is None or time.time() - last_log > \
                self.config.minimum_log_interval_secs:
                mean_train_metrics = {
                    k: sum(v) / len(v) for k, v in batch_metrics.items()
                }
                mean_train_metrics['counters/examples'] = self.example_counter
                mean_train_metrics['counters/updates'] = self.batch_counter
                rank0_print(
                    f'train stats after {self.example_counter} examples: ' + \
                    f'{formatted_dict(mean_train_metrics)}'
                )

                if self.config.wandb.enabled and self.rank == 0:
                    wandb.log(mean_train_metrics, step=self.example_counter)

                last_log = time.time()
            else:
                rank0_print(f'skipping logging after {self.example_counter} examples to avoid logging too frequently')
            #### END TRAINING ####

    def clip_gradient(self):
        """Clip the gradient norm of the parameters of a non-FSDP policy."""
        return torch.nn.utils.clip_grad_norm_(
            self.policy.parameters(), self.config.max_grad_norm
        ).item()

    def write_state_dict(
        self, step: int, state: Dict[str, torch.Tensor],
        metrics: Dict, filename: str, dir_name: Optional[str] = None
    ) -> None:
        """Write a checkpoint to disk."""
        if dir_name is None:
            dir_name = os.path.join(self.run_dir, f'LATEST')

        os.makedirs(dir_name, exist_ok=True)
        output_path = os.path.join(dir_name, filename)
        rank0_print(f'writing checkpoint to {output_path}...')
        torch.save({
            'step_idx': step,
            'state': state,
            'metrics': metrics if metrics is not None else {},
        }, output_path)

    def save(self, output_dir: Optional[str] = None, metrics: Optional[Dict] = None):
        """Save policy, optimizer, and scheduler state to disk."""

        policy_state_dict = self.policy.state_dict()
        self.write_state_dict(
            self.example_counter,
            policy_state_dict,
            metrics,
            'policy.pt',
            output_dir
        )
        del policy_state_dict

        optimizer_state_dict = self.optimizer.state_dict()
        self.write_state_dict(
            self.example_counter,
            optimizer_state_dict,
            metrics,
            'optimizer.pt',
            output_dir
        )
        del optimizer_state_dict

        scheduler_state_dict = self.scheduler.state_dict()
        self.write_state_dict(
            self.example_counter,
            scheduler_state_dict,
            metrics,
            'scheduler.pt',
            output_dir
        )


class FSDPTrainer(BasicTrainer):
    def __init__(
        self,
        policy: nn.Module,
        config: DictConfig,
        seed: int,
        run_dir: str,
        reference_model: Optional[nn.Module] = None,
        rank: int = 0,
        world_size: int = 1
    ) -> None:
        """A trainer subclass that uses PyTorch FSDP to shard the model across
        multiple GPUs.
        
        This trainer will shard both the policy and reference model across all 
        available GPUs. Models are sharded at the block level, where the block 
        class name is provided in the config.
        """

        super().__init__(
            policy, config, seed, run_dir, reference_model, rank, world_size
        )
        assert config.model.block_name is not None, \
            'must specify model.block_name ' + \
            '(e.g., GPT2Block or GPTNeoXLayer) for FSDP'

        wrap_class = get_block_class_from_model(policy, config.model.block_name)
        model_auto_wrap_policy = functools.partial(
            transformer_auto_wrap_policy,
            transformer_layer_cls={wrap_class},
        )

        shared_fsdp_kwargs = dict(
            auto_wrap_policy=model_auto_wrap_policy,
            sharding_strategy=ShardingStrategy.FULL_SHARD,
            cpu_offload=CPUOffload(offload_params=False),
            backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
            device_id=rank,
            ignored_modules=None,
            limit_all_gathers=False,
            use_orig_params=False,
            sync_module_states=False
        )

        rank0_print('Sharding policy...')
        mp_dtype = getattr(
            torch, config.model.fsdp_policy_mp
        ) if config.model.fsdp_policy_mp is not None else None
        policy_mp_policy = MixedPrecision(
            param_dtype=mp_dtype, reduce_dtype=mp_dtype, buffer_dtype=mp_dtype
        )
        self.policy = FSDP(
            policy, **shared_fsdp_kwargs, mixed_precision=policy_mp_policy
        )

        if config.activation_checkpointing:
            rank0_print('Attempting to enable activation checkpointing...')
            try:
                # use activation checkpointing, according to:
                # https://pytorch.org/blog/
                #scaling-multimodal-foundation-models-in-torchmultimodal/
                #-with-pytorch-distributed/
                # first, verify we have FSDP activation support ready by 
                # importing:
                from \
                torch.distributed.algorithms._checkpoint.checkpoint_wrapper \
                import (
                    checkpoint_wrapper,
                    apply_activation_checkpointing,
                    CheckpointImpl,
                )
                non_reentrant_wrapper = functools.partial(
                    checkpoint_wrapper,
                    offload_to_cpu=False,
                    checkpoint_impl=CheckpointImpl.NO_REENTRANT,
                )
            except Exception as e:
                rank0_print('FSDP activation checkpointing not available:', e)
            else:
                check_fn = lambda submodule: isinstance(submodule, wrap_class)
                rank0_print(
                    'Applying activation checkpointing wrapper to policy...'
                )
                apply_activation_checkpointing(
                    self.policy,
                    checkpoint_wrapper_fn=non_reentrant_wrapper,
                    check_fn=check_fn
                )
                rank0_print('FSDP activation checkpointing enabled!')

        if config.loss.name in {'dpo', 'ipo'}:
            rank0_print('Sharding reference model...')
            self.reference_model = FSDP(reference_model, **shared_fsdp_kwargs)

        print('Loaded model on rank', rank)
        dist.barrier()

    def clip_gradient(self):
        """Clip the gradient norm of the parameters of an FSDP policy,
           gathering the gradients across all GPUs.
        """
        return self.policy.clip_grad_norm_(self.config.max_grad_norm).item()

    def save(self, output_dir=None, metrics=None):
        """Save policy, optimizer, and scheduler state to disk, gathering from all processes and saving only on the rank 0 process."""
        save_policy = FullStateDictConfig(offload_to_cpu=True, rank0_only=True)
        with FSDP.state_dict_type(
            self.policy, StateDictType.FULL_STATE_DICT,
            state_dict_config=save_policy
        ):
            policy_state_dict = self.policy.state_dict()

        if self.rank == 0:
            self.write_state_dict(
                self.example_counter,
                policy_state_dict,
                metrics,
                'policy.pt',
                output_dir
            )
        del policy_state_dict
        dist.barrier()

        save_policy = FullOptimStateDictConfig(
            offload_to_cpu=True, rank0_only=True
        )
        with FSDP.state_dict_type(
            self.policy,
            StateDictType.FULL_STATE_DICT,
            optim_state_dict_config=save_policy
        ):
            optimizer_state_dict = FSDP.optim_state_dict(
                self.policy, self.optimizer
            )

        if self.rank == 0:
            self.write_state_dict(
                self.example_counter,
                optimizer_state_dict,
                metrics,
                'optimizer.pt',
                output_dir
            )
        del optimizer_state_dict
        dist.barrier()

        if self.rank == 0:
            scheduler_state_dict = self.scheduler.state_dict()
            self.write_state_dict(
                self.example_counter,
                scheduler_state_dict,
                metrics,
                'scheduler.pt',
                output_dir
            )
        dist.barrier()


class TensorParallelTrainer(BasicTrainer):
    def __init__(self, policy, config, seed, run_dir, reference_model=None, rank=0, world_size=1):
        """A trainer subclass that uses TensorParallel to shard the model 
        across multiple GPUs.

        Based on https://github.com/BlackSamorez/tensor_parallel. Note sampling 
        is extremely slow, see 
        https://github.com/BlackSamorez/tensor_parallel/issues/66.
        """
        super().__init__(
            policy, config, seed, run_dir, reference_model, rank, world_size
        )

        rank0_print('Sharding policy...')
        self.policy = tp.tensor_parallel(policy, sharded=True)
        if config.loss.name in {'dpo', 'ipo'}:
            rank0_print('Sharding reference model...')
            self.reference_model = tp.tensor_parallel(
                reference_model, sharded=False
            )

    def save(self, output_dir=None, metrics=None):
        """Save (unsharded) policy state to disk."""
        with tp.save_tensor_parallel(self.policy):
            policy_state_dict = self.policy.state_dict()

        self.write_state_dict(
            self.example_counter,
            policy_state_dict,
            metrics,
            'policy.pt',
            output_dir
        )
        del policy_state_dict
