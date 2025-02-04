
# -*- coding: utf-8 -*-
# Copyright (c) 2024, Songlin Yang, Yu Zhang

from typing import Optional, Tuple

import torch
import triton
import triton.language as tl


@triton.heuristics({
    'USE_OFFSETS': lambda args: args['offsets'] is not None
})
@triton.autotune(
    configs=[
        triton.Config({'BK': BK}, num_warps=num_warps, num_stages=num_stages)
        for BK in [32, 64]
        for num_warps in [1, 2, 4, 8]
        for num_stages in [2, 3, 4]
    ],
    key=["BC"]
)
@triton.jit
def chunk_dplr_delta_rule_fwd_A_kernel_intra_sub_inter(
    q,
    k,
    a,
    b,
    g, # cumsum
    g_original, #before cumsum
    Aqk,
    Aqb,
    Aab,
    Aak,
    offsets,
    indices,
    scale,
    T: tl.constexpr,
    H: tl.constexpr,
    K: tl.constexpr,
    BT: tl.constexpr,
    BC: tl.constexpr,
    BK: tl.constexpr,
    NC: tl.constexpr,
    USE_OFFSETS: tl.constexpr,
    HEAD_FIRST: tl.constexpr
):
    i_t, i_c, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    i_b, i_h = i_bh // H, i_bh % H
    i_i, i_j = i_c // NC, i_c % NC
    if USE_OFFSETS:
        i_n, i_t = tl.load(indices + i_t * 2).to(tl.int32), tl.load(indices + i_t * 2 + 1).to(tl.int32)
        bos, eos = tl.load(offsets + i_n).to(tl.int32), tl.load(offsets + i_n + 1).to(tl.int32)
        T = eos - bos
    else:
        bos, eos = i_b * T, i_b * T + T

    if i_t * BT + i_i * BC >= T:
        return
    if i_i <= i_j:
        return

    b_Aqk = tl.zeros([BC, BC], dtype=tl.float32)
    b_Aqb = tl.zeros([BC, BC], dtype=tl.float32)
    b_Aab = tl.zeros([BC, BC], dtype=tl.float32)
    b_Aak = tl.zeros([BC, BC], dtype=tl.float32)
    for i_k in range(tl.cdiv(K, BK)):
        o_k = i_k * BK + tl.arange(0, BK)
        m_k = o_k < K

        if HEAD_FIRST:
            p_q = tl.make_block_ptr(q + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, i_k * BK), (BC, BK), (1, 0))
            p_a = tl.make_block_ptr(a + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, i_k * BK), (BC, BK), (1, 0))
            p_gq = tl.make_block_ptr(g + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, i_k * BK), (BC, BK), (1, 0))
            p_gq_original = tl.make_block_ptr(g_original + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, i_k * BK), (BC, BK), (1, 0))
            p_k = tl.make_block_ptr(k + i_bh * T*K, (K, T), (1, K), (i_k * BK, i_t * BT + i_j * BC), (BK, BC), (0, 1))
            p_b = tl.make_block_ptr(b + i_bh * T*K, (K, T), (1, K), (i_k * BK, i_t * BT + i_j * BC), (BK, BC), (0, 1))
            p_gk = tl.make_block_ptr(g + i_bh * T*K, (K, T), (1, K), (i_k * BK, i_t * BT + i_j * BC), (BK, BC), (0, 1))
            p_gn = tl.max_contiguous(tl.multiple_of(g + (i_bh * T + i_t * BT + i_i * BC - 1) * K + o_k, BK), BK)
        else:
            p_q = tl.make_block_ptr(q + (bos*H+i_h)*K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, i_k * BK), (BC, BK), (1, 0))
            p_a = tl.make_block_ptr(a + (bos*H+i_h)*K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, i_k * BK), (BC, BK), (1, 0))
            p_gq = tl.make_block_ptr(g + (bos*H+i_h)*K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, i_k * BK), (BC, BK), (1, 0))
            p_gq_original = tl.make_block_ptr(g_original + (bos*H+i_h)*K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, i_k * BK), (BC, BK), (1, 0))
            p_k = tl.make_block_ptr(k + (bos*H+i_h)*K, (K, T), (1, H*K), (i_k * BK, i_t * BT + i_j * BC), (BK, BC), (0, 1))
            p_b = tl.make_block_ptr(b + (bos*H+i_h)*K, (K, T), (1, H*K), (i_k * BK, i_t * BT + i_j * BC), (BK, BC), (0, 1))
            p_gk = tl.make_block_ptr(g + (bos*H+i_h)*K, (K, T), (1, H*K), (i_k * BK, i_t * BT + i_j * BC), (BK, BC), (0, 1))
            p_gn = tl.max_contiguous(tl.multiple_of(g + (bos + i_t * BT + i_i * BC - 1) * H*K + i_h * K + o_k, BK), BK)
        # [BK,]
        b_gn = tl.load(p_gn, mask=m_k, other=0)
        # [BC, BK]
        b_q = tl.load(p_q, boundary_check=(0, 1))
        b_a = tl.load(p_a, boundary_check=(0, 1))
        b_gq = tl.load(p_gq, boundary_check=(0, 1))
        b_gq_original = tl.load(p_gq_original, boundary_check=(0, 1))
        b_ag = b_a * tl.exp(b_gq - b_gq_original - b_gn[None, :])
        b_qg = b_q * tl.exp(b_gq - b_gn[None, :]) * scale
        # [BK, BC]
        b_k = tl.load(p_k, boundary_check=(0, 1))
        b_b = tl.load(p_b, boundary_check=(0, 1))
        b_gk = tl.load(p_gk, boundary_check=(0, 1))
        tmp = tl.exp(b_gn[:, None] - b_gk)
        b_kg = b_k * tmp
        b_bg = b_b * tmp
        # [BC, BC] using tf32 to improve precision here.
        b_Aab += tl.dot(b_ag, b_bg)
        b_Aak += tl.dot(b_ag, b_kg)
        b_Aqk += tl.dot(b_qg, b_kg)
        b_Aqb += tl.dot(b_qg, b_bg)

    if HEAD_FIRST:
        p_Aqk = tl.make_block_ptr(Aqk + i_bh*T*BT, (T, BT), (BT, 1), (i_t * BT + i_i * BC, i_j * BC), (BC, BC), (1, 0))
        p_Aqb = tl.make_block_ptr(Aqb + i_bh*T*BT, (T, BT), (BT, 1), (i_t * BT + i_i * BC, i_j * BC), (BC, BC), (1, 0))
        p_Aab = tl.make_block_ptr(Aab + i_bh*T*BT, (T, BT), (BT, 1), (i_t * BT + i_i * BC, i_j * BC), (BC, BC), (1, 0))
        p_Aak = tl.make_block_ptr(Aak + i_bh*T*BT, (T, BT), (BT, 1), (i_t * BT + i_i * BC, i_j * BC), (BC, BC), (1, 0))
    else:
        p_Aqk = tl.make_block_ptr(Aqk + (bos*H+i_h)*BT, (T, BT), (H*BT, 1), (i_t * BT + i_i * BC, i_j * BC), (BC, BC), (1, 0))
        p_Aqb = tl.make_block_ptr(Aqb + (bos*H+i_h)*BT, (T, BT), (H*BT, 1), (i_t * BT + i_i * BC, i_j * BC), (BC, BC), (1, 0))
        p_Aab = tl.make_block_ptr(Aab + (bos*H+i_h)*BT, (T, BT), (H*BT, 1), (i_t * BT + i_i * BC, i_j * BC), (BC, BC), (1, 0))
        p_Aak = tl.make_block_ptr(Aak + (bos*H+i_h)*BT, (T, BT), (H*BT, 1), (i_t * BT + i_i * BC, i_j * BC), (BC, BC), (1, 0))
    tl.store(p_Aqk, b_Aqk.to(Aqk.dtype.element_ty), boundary_check=(0, 1))
    tl.store(p_Aqb, b_Aqb.to(Aqb.dtype.element_ty), boundary_check=(0, 1))
    tl.store(p_Aab, b_Aab.to(Aab.dtype.element_ty), boundary_check=(0, 1))
    tl.store(p_Aak, b_Aak.to(Aak.dtype.element_ty), boundary_check=(0, 1))



@triton.heuristics({
    'USE_OFFSETS': lambda args: args['offsets'] is not None
})
@triton.autotune(
    configs=[
        triton.Config({}, num_warps=1),
        triton.Config({}, num_warps=2),
        triton.Config({}, num_warps=4),
        triton.Config({}, num_warps=8),
    ],
    key=["BK", "BT"]
)
@triton.jit
def chunk_dplr_delta_rule_fwd_A_kernel_intra_sub_intra(
    q,
    k,
    a,
    b,
    g,
    g_original,
    qg,
    kg,
    ag,
    bg,
    Aqk,
    Aqb,
    Aab,
    Aak,
    offsets,
    indices,
    scale,
    T: tl.constexpr,
    H: tl.constexpr,
    K: tl.constexpr,
    BT: tl.constexpr,
    BC: tl.constexpr,
    BK: tl.constexpr,
    NC: tl.constexpr,
    USE_OFFSETS: tl.constexpr,
    HEAD_FIRST: tl.constexpr
):
    i_t, i_i, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    i_b, i_h = i_bh // H, i_bh % H
    i_j = i_i
    if USE_OFFSETS:
        i_n, i_t = tl.load(indices + i_t * 2).to(tl.int32), tl.load(indices + i_t * 2 + 1).to(tl.int32)
        bos, eos = tl.load(offsets + i_n).to(tl.int32), tl.load(offsets + i_n + 1).to(tl.int32)
        T = eos - bos
    else:
        bos, eos = i_b * T, i_b * T + T

    if i_t * BT + i_i * BC >= T:
        return

    o_i = tl.arange(0, BC)
    o_k = tl.arange(0, BK)
    m_k = o_k < K
    m_A = (i_t * BT + i_i * BC + tl.arange(0, BC)) < T
    last_idx = min((i_t+1) * BT, T) - 1
    if HEAD_FIRST:
        o_A = i_bh * T*BT + (i_t * BT + i_i * BC + tl.arange(0, BC)) * BT + i_j * BC
        p_q = tl.make_block_ptr(q + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_k = tl.make_block_ptr(k + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_a = tl.make_block_ptr(a + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_b = tl.make_block_ptr(b + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_g = tl.make_block_ptr(g + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_g_original = tl.make_block_ptr(g_original + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_g_last = g + i_bh * T*K + last_idx * K + tl.arange(0, BK)
        b_g_last = tl.load(p_g_last, mask=m_k, other=0)

        p_qg = tl.make_block_ptr(qg + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_kg = tl.make_block_ptr(kg + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_ag = tl.make_block_ptr(ag + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_bg = tl.make_block_ptr(bg + i_bh * T*K, (T, K), (K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
    else:
        o_A = (bos + i_t * BT + i_i * BC + tl.arange(0, BC)) * H*BT + i_h * BT + i_j * BC
        p_q = tl.make_block_ptr(q + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_k = tl.make_block_ptr(k + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_a = tl.make_block_ptr(a + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_b = tl.make_block_ptr(b + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_g = tl.make_block_ptr(g + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_g_original = tl.make_block_ptr(g_original + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_g_last = g + (bos * H + i_h) * K + last_idx * H * K + tl.arange(0, BK)
        b_g_last = tl.load(p_g_last, mask=m_k, other=0)
        p_qg = tl.make_block_ptr(qg + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_kg = tl.make_block_ptr(kg + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_ag = tl.make_block_ptr(ag + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))
        p_bg = tl.make_block_ptr(bg + (bos * H + i_h) * K, (T, K), (H*K, 1), (i_t * BT + i_i * BC, 0), (BC, BK), (1, 0))

    b_q = tl.load(p_q, boundary_check=(0, 1))
    b_q = b_q * scale
    b_k = tl.load(p_k, boundary_check=(0, 1))
    b_a = tl.load(p_a, boundary_check=(0, 1))
    b_b = tl.load(p_b, boundary_check=(0, 1))
    b_g = tl.load(p_g, boundary_check=(0, 1))
    b_g_original = tl.load(p_g_original, boundary_check=(0, 1))

    ## deal with decay term.
    g_exp = tl.exp(b_g)
    g_exp_inv = tl.exp(-b_g + b_g_last[None, :])
    b_qg = b_q * g_exp
    b_kg = b_k * g_exp_inv
    b_bg = b_b * g_exp_inv
    b_ag = b_a * tl.exp(b_g - b_g_original)
    tl.store(p_qg, b_qg.to(p_qg.dtype.element_ty), boundary_check=(0, 1))
    tl.store(p_bg, b_bg.to(p_bg.dtype.element_ty), boundary_check=(0, 1))
    tl.store(p_ag, b_ag.to(p_ag.dtype.element_ty), boundary_check=(0, 1))
    tl.store(p_kg, b_kg.to(p_kg.dtype.element_ty), boundary_check=(0, 1))
    b_qg, b_kg, b_ag, b_bg = None, None, None, None
    tl.debug_barrier()

    bg_exclusive = b_g - b_g_original
    b_q = b_q.to(b_k.dtype)
    # inner attn
    for j in range(0, min(BC, T - i_t * BT - i_i * BC)):
        # a trick to index the j-th row of b_k, b_g, b_b
        mask = tl.arange(0, BC) == j
        b_k_j = tl.sum(tl.where(mask[:, None], b_k, 0), 0)
        b_gk_j = tl.sum(tl.where(mask[:, None], b_g, 0), 0)
        b_b_j = tl.sum(tl.where(mask[:, None], b_b, 0), 0)
    
        tmp = tl.exp(b_g - b_gk_j[None, :])
        b_A_qk = tl.sum(b_q * b_k_j[None, :] * tmp, 1)
        b_A_qk = tl.where(o_i >= j, b_A_qk, 0.)
        b_A_qb = tl.sum(b_q * b_b_j[None] * tmp, 1)
        b_A_qb = tl.where(o_i >= j, b_A_qb, 0.)
        tmp2 = tl.exp(bg_exclusive - b_gk_j[None, :])
        b_A_ak = tl.sum(b_a * b_k_j[None, :] * tmp2, 1)
        b_A_ak = tl.where(o_i > j, b_A_ak, 0.)
        b_A_ab = tl.sum(b_a * b_b_j[None, :] * tmp2, 1)
        b_A_ab = tl.where(o_i > j, b_A_ab, 0.)
        tl.store(Aqk + o_A + j, b_A_qk, mask=m_A)
        tl.store(Aqb + o_A + j, b_A_qb, mask=m_A)
        tl.store(Aab + o_A + j, b_A_ab, mask=m_A)
        tl.store(Aak + o_A + j, b_A_ak, mask=m_A)



def chunk_fwd_intra_dplr_fn(
        q: torch.Tensor,
        k: torch.Tensor,
        a: torch.Tensor,
        b: torch.Tensor,
        g: torch.Tensor,
        g_original: torch.Tensor,
        scale: float,
        BT: int,
        offsets: Optional[torch.LongTensor] = None,
        indices: Optional[torch.LongTensor] = None,
        head_first: bool = True,
    ):
    if head_first:
        B, H, T, K = k.shape
    else:
        B, T, H, K = k.shape
    BT = min(BT, max(16, triton.next_power_of_2(T)))
    NT = triton.cdiv(T, BT) if offsets is None else len(indices)
    BC = min(16, BT)
    NC = triton.cdiv(BT, BC)

    Aqk = q.new_empty(B, *((H, T) if head_first else (T, H)), BT, dtype=q.dtype)
    Aqb = q.new_empty(B, *((H, T) if head_first else (T, H)), BT, dtype=q.dtype)
    # involving matrix inverse and it'd be better to use float here.
    Aab = q.new_empty(B, *((H, T) if head_first else (T, H)), BT, dtype=torch.float)
    Aak = q.new_empty(B, *((H, T) if head_first else (T, H)), BT, dtype=torch.float)
    grid = (NT, NC * NC, B * H)
    chunk_dplr_delta_rule_fwd_A_kernel_intra_sub_inter[grid](
        q=q, k=k, a=a, b=b, g=g, g_original=g_original, Aqk=Aqk, Aqb=Aqb, Aab=Aab, Aak=Aak,
        offsets=offsets, indices=indices,
        scale=scale,
        T=T, H=H, K=K, BT=BT, BC=BC, NC=NC,
        HEAD_FIRST=head_first
    )
    grid = (NT, NC, B * H)
    BK = triton.next_power_of_2(K)
    qg = torch.empty_like(q)
    kg = torch.empty_like(k)
    ag = torch.empty_like(a)
    bg = torch.empty_like(b)
    chunk_dplr_delta_rule_fwd_A_kernel_intra_sub_intra[grid](
        q=q, k=k, a=a, b=b, g=g, g_original=g_original, Aqk=Aqk, Aqb=Aqb, Aab=Aab, Aak=Aak,
        qg=qg, kg=kg, ag=ag, bg=bg,
        offsets=offsets, indices=indices,
        scale=scale,
        T=T, H=H, K=K, BT=BT, BC=BC, BK=BK, HEAD_FIRST=head_first, NC=NC
    )
    return Aab, Aqk, Aak, Aqb, qg, kg, ag, bg



from einops import rearrange
def chunk_fwd_intra_dplr_fn_reference(q, k, a, b, gk, gk_cumsum, scale, chunk_size):
    mask = torch.triu(torch.ones(chunk_size, chunk_size, dtype=torch.bool, device=q.device), diagonal=0)
    B, H, L, D = q.shape
    q, k, a, b, gk, gk_cumsum = map(lambda x: rearrange(x, 'b h (n c) d -> b h n c d', c=chunk_size).float(), [q, k, a, b, gk, gk_cumsum])
    # v2 = (alpha @ k.transpose(-1, -2)).masked_fill_(mask, 0) @ v
    A_ab = torch.zeros(B, H, L // chunk_size, chunk_size, chunk_size).to(q.device)
    A_qk = torch.zeros(B, H, L // chunk_size, chunk_size, chunk_size).to(q.device)
    A_ak = torch.zeros(B, H, L // chunk_size, chunk_size, chunk_size).to(q.device)
    A_qb = torch.zeros(B, H, L // chunk_size, chunk_size, chunk_size).to(q.device)

    for i in range(chunk_size):
        a_i = a[:, :, :, i, None]
        q_i = q[:, :, :, i, None]
        gk_i = gk_cumsum[:, :, :, i, None]
        g_i = gk[:, :, :, i, None]
        mask = (torch.arange(chunk_size) <= i).to(q.device)
        attn_i = (gk_i - gk_cumsum).masked_fill(~mask.unsqueeze(-1), float('-inf')).exp()
        A_qk[:, :, :, i, :] = (q_i * k * attn_i).sum(-1).clone()
        A_qb[:, :, :, i, :] = (q_i * b * attn_i).sum(-1).clone()
        mask = (torch.arange(chunk_size) < i).to(q.device)
        # shift by one.
        attn_i = (gk_i - g_i - gk_cumsum).masked_fill(~mask.unsqueeze(-1), float('-inf')).exp()
        A_ab[:, :, :, i, :] = (a_i * b * attn_i).sum(-1).clone()
        A_ak[:, :, :, i, :] = (a_i * k * attn_i).sum(-1).clone()
    return map(lambda x: rearrange(x, 'b h n c d -> b h (n c) d', c=chunk_size), [A_ab, A_qk, A_ak, A_qb])


if __name__ == '__main__':
    torch.manual_seed(42)
    import os
    os.environ['TRITON_F32_DEFAULT'] = 'ieee'

    B, H, L, D = 1, 2, 1024, 128
    q = torch.randn(B, H, L, D)
    k = torch.randn(B, H, L, D)
    a = torch.randn(B, H, L, D)
    b = torch.randn(B, H, L, D)
    from torch.nn import functional as F
    gk = F.logsigmoid(torch.randn(B, H, L, D)) / 16
    gk_cumsum = gk.cumsum(-2)
    q, k, a, b, gk, gk_cumsum = map(lambda x: x.cuda(), [q, k, a, b, gk, gk_cumsum])
    scale = 1.0
    chunk_size = 64
    A_ab_triton, A_qk_triton, A_ak_triton, A_qb_triton, _, _, _, _ = chunk_fwd_intra_dplr_fn(q.clone().contiguous(), k.clone().contiguous(), a.clone().contiguous(), b.clone().contiguous(), gk_cumsum.clone().contiguous(), gk.clone().contiguous(), scale, chunk_size)
    A_ab, A_qk, A_ak, A_qb = chunk_fwd_intra_dplr_fn_reference(q.clone().contiguous(), k.clone().contiguous(), a.clone().contiguous(), b.clone().contiguous(), gk.clone().contiguous(), gk_cumsum.clone().contiguous(), scale, chunk_size)
    print(A_ab_triton.shape, A_qk_triton.shape, A_ak_triton.shape, A_qb_triton.shape)
    print(A_ab.shape, A_qk.shape, A_ak.shape, A_qb.shape)
    print((A_ab - A_ab_triton).abs().max())
    print((A_qk - A_qk_triton).abs().max())
    print((A_ak - A_ak_triton).abs().max())
    print((A_qb - A_qb_triton).abs().max())
    breakpoint()

