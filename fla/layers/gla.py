# -*- coding: utf-8 -*-

# "Gated Linear Attention Transformers with Hardware-Efficient Training"[https://arxiv.org/abs/2312.06635]

from __future__ import annotations

from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from transformers.activations import ACT2FN
from transformers.cache_utils import Cache

from fla.modules import FusedRMSNormSwishGate, RMSNorm, ShortConvolution
from fla.ops.gla import chunk_gla, fused_chunk_gla, fused_recurrent_gla


class GatedLinearAttention(nn.Module):

    def __init__(
        self,
        mode: str = 'fused_chunk',
        hidden_size: int = 1024,
        expand_k: float = 1.0,
        expand_v: float = 2.0,
        num_heads: int = 4,
        use_short_conv: bool = False,
        conv_size: int = 4,
        conv_bias: bool = False,
        share_conv_kernel: bool = True,
        gate_fn: str = 'swish',
        layernorm_eps: float = 1e-5,
        gate_logit_normalizer: int = 16,
        gate_low_rank_dim: int = 16,
        clamp_min: Optional[float] = None,
        fuse_norm: bool = True,
        layer_idx: int = None,
        **kwargs
    ) -> GatedLinearAttention:
        super().__init__()

        self.mode = mode
        self.hidden_size = hidden_size
        self.expand_k = expand_k
        self.expand_v = expand_v
        self.num_heads = num_heads

        self.use_short_conv = use_short_conv
        self.conv_size = conv_size
        self.conv_bias = conv_bias
        self.share_conv_kernel = share_conv_kernel

        self.key_dim = int(hidden_size * expand_k)
        self.value_dim = int(hidden_size * expand_v)
        self.clamp_min = clamp_min
        self.layer_idx = layer_idx

        assert mode in ['chunk', 'fused_recurrent', 'fused_chunk'], f"Not suppoerted mode `{mode}`."
        assert self.key_dim % num_heads == 0, f"key dim must be divisible by num_heads of {num_heads}"
        assert self.value_dim % num_heads == 0, f"value dim must be divisible by num_heads of {num_heads}"

        self.head_qk_dim = self.key_dim // num_heads
        self.head_v_dim = self.value_dim // num_heads

        self.q_proj = nn.Linear(hidden_size, self.key_dim, bias=False)
        self.k_proj = nn.Linear(hidden_size, self.key_dim, bias=False)
        self.v_proj = nn.Linear(hidden_size, self.value_dim, bias=False)
        self.g_proj = nn.Linear(hidden_size, self.value_dim, bias=False)

        if use_short_conv:
            self.conv_size = conv_size
            if share_conv_kernel:
                self.h_conv1d = ShortConvolution(hidden_size, conv_size, activation='silu')
            else:
                self.q_conv1d = ShortConvolution(self.key_dim, conv_size, activation='silu')
                self.k_conv1d = ShortConvolution(self.key_dim, conv_size, activation='silu')
                self.v_conv1d = ShortConvolution(self.value_dim, conv_size, activation='silu')

        self.gk_proj = nn.Sequential(nn.Linear(hidden_size, gate_low_rank_dim, bias=False),
                                     nn.Linear(gate_low_rank_dim, self.key_dim, bias=True))
        self.o_proj = nn.Linear(self.value_dim, hidden_size, bias=False)

        if gate_fn == 'swish' and fuse_norm:
            self.g_norm_swish_gate = FusedRMSNormSwishGate(self.head_v_dim, eps=layernorm_eps)
            self.fuse_norm_and_gate = True
        else:
            self.fuse_norm_and_gate = False
            self.g_norm = RMSNorm(self.head_v_dim, eps=layernorm_eps)
            self.gate_fn = ACT2FN[gate_fn]

        self.gate_logit_normalizer = gate_logit_normalizer

        self.apply(self._initialize_weights)

    def _initialize_weights(self, module: nn.Module):
        if getattr(module, "_is_hf_initialized", False):
            return
        if isinstance(module, nn.Linear):
            nn.init.xavier_uniform_(module.weight, gain=2 ** -2.5)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        module._is_hf_initialized = True

    def forward(
        self,
        hidden_states: torch.Tensor,
        past_key_values: Optional[Cache] = None,
        use_cache: Optional[bool] = False,
        output_attentions: Optional[bool] = False,
        **kwargs
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Cache]]:
        # launching the triton kernel for just one token will actually be slower
        mode = 'fused_recurrent' if hidden_states.shape[1] == 1 else self.mode

        last_state = past_key_values[self.layer_idx] if use_cache else None
        if self.use_short_conv:
            conv_state = last_state[0] if use_cache else None
            if self.share_conv_kernel:
                # conv state is updated inplace
                hidden_states = self.h_conv1d(hidden_states, conv_state)
                q = self.q_proj(hidden_states)
                k = self.k_proj(hidden_states)
                v = self.v_proj(hidden_states)
            else:
                conv_state_q = last_state[0] if use_cache else None
                conv_state_k = last_state[1] if use_cache else None
                conv_state_v = last_state[2] if use_cache else None
                q = self.q_conv1d(self.q_proj(hidden_states), conv_state_q)
                k = self.k_conv1d(self.k_proj(hidden_states), conv_state_k)
                v = self.v_conv1d(self.v_proj(hidden_states), conv_state_v)
        else:
            q = self.q_proj(hidden_states)
            k = self.k_proj(hidden_states)
            v = self.v_proj(hidden_states)

        q, k, v = map(lambda x: rearrange(x, 'b l (h d) -> b h l d', h=self.num_heads), (q, k, v))
        gk = rearrange(self.gk_proj(hidden_states), 'b n (h d) -> b h n d', h=self.num_heads)
        gk = F.logsigmoid(gk) / self.gate_logit_normalizer

        if self.clamp_min is not None:
            gk = torch.clamp_min(gk, self.clamp_min)

        recurrent_state = last_state[-1] if use_cache else None
        if mode == 'fused_recurrent':
            o, recurrent_state = fused_recurrent_gla(q, k, v, gk, initial_state=recurrent_state, output_final_state=use_cache)
        elif mode == 'fused_chunk':
            o, recurrent_state = fused_chunk_gla(q, k, v, gk, initial_state=recurrent_state, output_final_state=use_cache)
        elif mode == 'chunk':
            o, recurrent_state = chunk_gla(q, k, v, gk, initial_state=recurrent_state, output_final_state=use_cache)
        else:
            raise NotImplementedError(f"Not supported mode `{mode}`.")

        if past_key_values is not None:
            if self.use_short_conv:
                if self.share_conv_kernel:
                    last_state = (conv_state, recurrent_state)
                else:
                    last_state = (conv_state_q, conv_state_k, conv_state_v, recurrent_state)
            else:
                last_state = (recurrent_state,)
            past_key_values.update(last_state, self.layer_idx, q.shape[2])

        o = rearrange(o, 'b h l d -> b l h d')
        g = self.g_proj(hidden_states)
        if self.fuse_norm_and_gate:
            g = rearrange(g, 'b l (h d) -> b l h d', h=self.num_heads)
            o = self.g_norm_swish_gate(o, g)
            o = rearrange(o, 'b l h d -> b l (h d)')
        else:
            o = self.g_norm(o)
            o = rearrange(o, 'b l h d -> b l (h d)')
            o = o * self.gate_fn(g)
        o = self.o_proj(o)

        return o, None, past_key_values

    def init_state(self, batch_size: int) -> Tuple[torch.Tensor]:
        param = next(self.parameters())
        state = tuple()
        if self.use_short_conv:
            if self.share_conv_kernel:
                state += (param.new_zeros(batch_size, self.hidden_size, self.conv_size),)
            else:
                state += (param.new_zeros(batch_size, self.key_dim, self.conv_size),
                          param.new_zeros(batch_size, self.key_dim, self.conv_size),
                          param.new_zeros(batch_size, self.value_dim, self.conv_size))
        state += (param.new_zeros(batch_size, self.num_heads, self.head_qk_dim, self.head_v_dim),)
        return state

    def state_size(self, **kwargs) -> int:
        state_size = self.key_dim * self.head_v_dim
        for module in self.children():
            if isinstance(module, ShortConvolution):
                state_size += module.state_size
        return state_size
