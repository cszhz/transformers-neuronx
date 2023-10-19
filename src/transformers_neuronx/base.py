# Copyright Amazon Web Services and its Affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import os
import torch
import hashlib
from transformers_neuronx import bucket
from transformers_neuronx import utils
from transformers_neuronx import module


# Mainly used to expose top level APIs to the model object for serialization
class NeuronModelBase(module.WrappingCheckpointCompatibleModel):
    is_fid = False

    # top level api
    def save(self, directory):
        assert self.serialization_enabled(), 'serialization is not enabled for this model'
        self._save_compiled_artifacts(directory)

    # top level api
    def load(self, directory):
        assert self.serialization_enabled(), 'serialization is not enabled for this model'
        self._load_compiled_artifacts(directory)

    # simple implementation that doesn't take into account cache and serialization
    def is_compiled(self):
        # First check if the kernels have neffs already
        try:
            if all([kernel.neff_bytes is not None for kernel in self._get_all_kernels()]):
                return True
        # AttributeError means kernels don't even exist yet.
        except AttributeError:
            pass
        return False
   
   #top level api
    def enable_speculative_decoder(self,k=4):
        self.decoder_lm_head_for_speculation=self.decoder_param_set.init_speculative_decoder(unroll=self.unroll, buckets=self.token_buckets, model_obj=self, n_active_tokens=k)

    def reorder_cache(self, reorder_ids):
        self.decoder_lm_head.program.reorder_cache(reorder_ids)

    def setup_reorder_cache(self):
        if self.decoder_lm_head.program is not None: # called after to_neuron
            self.decoder_lm_head.program.setup_reorder_cache()
        else:
            self.decoder_lm_head.need_reorder_cache = True

    def _save_compiled_artifacts(self, directory):
        if os.path.isfile(directory):
            raise FileExistsError(
                f'Artifacts should be saved to a directory. '
                f'Found existing file: {directory}'
            )
        os.makedirs(directory, exist_ok=True)
        for i, nbs_obj in enumerate(self.nbs_objs):
            nbs_obj.save_compiler_artifacts(directory)

    def _load_compiled_artifacts(self, directory):
        if not os.path.isdir(directory):
            raise FileNotFoundError(f'Did not find directory: {directory}.')

        for i, nbs_obj in enumerate(self.nbs_objs):
            nbs_obj.load_compiler_artifacts_after_build(directory)

    def _get_all_kernels(self):
        all_kernels = []
        for nbs in self.nbs_objs:
            for kernel in nbs.get_all_kernels():
                all_kernels.append(kernel)
        return all_kernels


    # To enable serialization, have the model call this
    # function to register all nbs_obj of your model.
    # The nbs_obj must follow 3 rules:
    #   1. The nbs_obj must inherit from NeuronBaseSerializer.
    #   2. Since this class shouldn't be used directly, a nbs_obj.get_all_kernels()
    #      method should be implemented by the child class, which returns a
    #      list of all kernels which have NEFFs.
    #   3. It must use:
    #      if nbs_obj.compiler_artifacts_path is not None:
    #          nbs_obj.set_neff_bytes()
    #      after its kernels have been created, but before they are compiled
    def register_for_serialization(self, nbs_obj):
        # check that at least requirement 1 and 2 are met, 3 is hard to check for here
        assert issubclass(type(nbs_obj), NeuronBaseSerializer), 'The nbs_obj must inheret from NeuronBaseSerializer.'
        assert getattr(nbs_obj, 'get_all_kernels', None) is not None, 'An nbs_obj.get_all_kernels() method should be implemented.'
        temp = getattr(self, 'nbs_objs', [])
        nbs_obj.compiler_artifacts_path = None
        temp.append(nbs_obj)
        self.nbs_objs = temp

    def reset(self):
        self.decoder_lm_head.reset()

    def context(self, hidden, cache_ids, start_ids, last_token_id, *rest):
        """A helper to process context (prompt)
        1) if there is available context encoding model (infered from self.context_buckets)
            - when context_length >= estimate, slice the context up to estimate,
                and call context encoding model
            - when context_length < estimate, skip and fall back to serial token generation model

            and mark `current` accrodingly

        2) process the left over tokens accroding to `current`
            - if there is no context encoding model, simply do serial token generation for context

        Other arguments that are required by the model are contained in `rest`.
        """
        context_length = hidden.shape[1]
        # batch_size is in dim 2 because of the transpose taken in _forward function
        batch_size = hidden.shape[2]

        if self.is_fid:
            # Fusion-In-Decoder context encoding
            fused_context_length = hidden.shape[1]
            context_length = fused_context_length // self.batch_size

        current = 0

        estimate = bucket.find(self.context_buckets, context_length)


        if estimate is not None:
            hidden_context = hidden
            cache_context = cache_ids

            # Slice context that when it is too large
            if context_length > estimate:
                current = estimate
                hidden_context = hidden[:, :estimate]
                cache_context = cache_ids[:estimate]

            # Cannot use context encoding for a context that is too small. This
            # is because the caller must be aware of the cache-ids/start-ids
            # used.
            elif context_length < estimate:
                raise ValueError(f"context_length ({context_length}) shouldn't be smaller than estimate ({estimate})")

            # Directly pass input to the context network when exactly sized
            else:
                current = estimate

            if current == estimate:
                model = self.decoder_lm_head_for_context[estimate, batch_size]
                logits = model(hidden_context, cache_context, start_ids, last_token_id, *rest)

        for i in range(current, context_length):
            cache_ids = torch.as_tensor([i], dtype=torch.int32)
            hidden_slice = hidden[:, i:i+1].contiguous()
            logits = self.decoder_lm_head(hidden_slice, cache_ids, start_ids, last_token_id, *rest)

        if self.is_fid:
            logits[:] = float('-inf')
            logits[self.bos_token_id] = 1.0

        return logits

    def _prepare_for_par_ctx_rhs_padding(self, input_ids):
        """A helper to do rhs padding on prompt for parallel context encoding model
        i.e.
            input_ids = [[111, 222, 333]]
            context_length = 3

            if context bucket size is 4
            we will pad input_ids to [[111, 222, 333, 0]]

            last_token_id = 2 (used for generation to mark the last token is at index 2 instead 3)

        Note:
            - there is no change on start_ids with right padding.
            - cache_ids will be set to [0, 1, 2, 3] in self.forward()
        """
        batch_size, context_length = input_ids.shape

        # if last_token_id not used, simply set to 0
        last_token_id = torch.as_tensor(0, dtype=torch.int32)
        if context_length == 1:
            return input_ids, last_token_id

        # TODO: check context_buckets for compatibility with OPT
        if hasattr(self, "context_buckets"):
            estimate = bucket.find(self.context_buckets, context_length)
        else:
            estimate = self.context_length_estimate

        if estimate:
            # when context length is larger than estimate, last_token_id=estimate-1
            last_token_id = torch.as_tensor(min(context_length - 1, estimate-1), dtype=torch.int32)
            if context_length < estimate:
                input_ids = utils.pad(input_ids, 1, estimate, left=False)

        return input_ids, last_token_id

    def _preprocess(self, input_ids, start_ids=None, cache_ids=None):
        # right pad the input_ids if neccessary
        input_ids, last_token_id = self._prepare_for_par_ctx_rhs_padding(input_ids)

        # note: this context_length is after right padded
        batch_size, context_length = input_ids.shape

        if start_ids is None:
            start_ids = torch.zeros(batch_size, dtype=torch.int32)

        if cache_ids is None:
            cache_ids = torch.arange(context_length, dtype=torch.int32)

        if hasattr(self, "prefixed_length") and self.prefixed_length:
            cache_ids += self.prefixed_length

        return input_ids, cache_ids, start_ids, last_token_id

    def _cast_logits(self, logits):
         # Cast logits to float32 or the dtype specified in the neuron config
         logits_dtype = torch.float32
         if self.neuron_config:
             logits_dtype = getattr(torch, self.neuron_config.cast_logits_dtype)
         return logits.to(logits_dtype)

    def _forward(self, hidden, *args):
        hidden = hidden.transpose(0, -1).contiguous()

        _, context_length, _ = hidden.shape

        if context_length > 1:
            logits = self.context(hidden, *args)
        else:
            logits = self.decoder_lm_head(hidden, *args)

        logits = logits.to(torch.float32)
        _,n_active_tokens,_=logits.shape
        if n_active_tokens>1:
            logits = logits[:self.config.vocab_size, -n_active_tokens:, :]
        else:
            logits = logits[:self.config.vocab_size, -1, :] 
        logits = logits.transpose(0, 1)
        return logits

    def serialization_enabled(self):
        return getattr(self, 'nbs_objs', None) is not None

# Base class for all "Serializable Objects"
class NeuronBaseSerializer:

    def save_compiler_artifacts(self, path):
        for kernel in self.get_all_kernels():
            hlo_hash = hash_hlo(kernel.hlo_module)
            with open(os.path.join(path, hlo_hash), 'wb') as f:
                assert kernel.neff_bytes is not None, "cannot save a model which has not been successfully compiled"
                f.write(kernel.neff_bytes)

    def load_compiler_artifacts_after_build(self, path):
        self.compiler_artifacts_path = path

    def set_neff_bytes(self):
        for kernel in self.get_all_kernels():
            hlo_hash = hash_hlo(kernel.hlo_module)
            try:
                with open(os.path.join(self.compiler_artifacts_path, hlo_hash), 'rb') as f:
                    kernel.neff_bytes = f.read()
            except FileNotFoundError:
                raise FileNotFoundError(('Could not find a matching NEFF for your HLO in this directory. '
                                          'Ensure that the model you are trying to load is the same type and '
                                          'has the same parameters as the one you saved or call "save" on '
                                          'this model to reserialize it.'))

    def get_all_kernels(self):
        raise NotImplementedError(
            f'Class {type(self)} deriving from NeuronBaseSerializer must implement get_all_kernels'
        )

def hash_hlo(hlo_module):
    hash_gen = hashlib.sha256()
    message = hlo_module.SerializeToString()
    hash_gen.update(message)
    hash = str(hash_gen.hexdigest())[:20]
    return hash + '.neff'
