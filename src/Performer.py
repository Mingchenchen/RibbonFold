import math
import torch
import torch.nn.functional as F
from torch import nn
import numpy as np

from functools import partial

# Original implementation from https://github.com/lucidrains/performer-pytorch

# helpers
def exists(val):
    return val is not None

def empty(tensor):
    return tensor.numel() == 0

def default(val, d):
    return val if exists(val) else d

def get_module_device(module):
    return next(module.parameters()).device

def find_modules(nn_module, type):
    return [module for module in nn_module.modules() if isinstance(module, type)]

# kernel functions
def softmax_kernel(data, *, projection_matrix, is_query, normalize_data=True, eps=1e-4, device = None):
    b, h, *_ = data.shape
    # q, k, v: (B*N, H, L, E/H)

    data_normalizer = (data.shape[-1] ** -0.25) if normalize_data else 1.

    # (nb_features, E/H)
    ratio = (projection_matrix.shape[0] ** -0.5)

    # (nb_features, E/H) -> (1, nb_features, E/H) -> (H, nb_features, E/H)
    projection = projection_matrix.unsqueeze(0).repeat(h, 1, 1)
    # (H, nb_features, E/H) -> (1, H, nb_features, E/H) -> (B*N, H, nb_features, E/H)
    projection = projection.unsqueeze(0).repeat(b, 1, 1, 1)
    projection = projection.type_as(data)

    # (B*N, H, L, E/H) @ (B*N, H, nb_features, E/H) -> (B*N, H, L, nb_features)
    data_dash = torch.einsum('...id,...jd->...ij', (data_normalizer * data), projection)

    diag_data = data ** 2 # (B*N, H, L, E/H)
    diag_data = torch.sum(diag_data, dim=-1) # (B*N, H, L, E/H) -> (B*N, H, L)
    diag_data = (diag_data / 2.0) * (data_normalizer ** 2) # (B*N, H, L)
    diag_data = diag_data.unsqueeze(dim=-1) # (B*N, H, L, 1)

    if is_query:
        data_dash = ratio * (
            # (B*N, H, L, nb_features) - (B*N, H, L, 1)  - (B*N, H, L, 1)
            torch.exp(data_dash - diag_data - torch.max(data_dash, dim=-1, keepdim=True).values) + eps)
    else:
        data_dash = ratio * (
            # (B*N, H, L, nb_features) - (B*N, H, L, 1) - (1,)
            torch.exp(data_dash - diag_data - torch.max(data_dash)) + eps)

    return data_dash.type_as(data)

def generalized_kernel(data, *, projection_matrix, kernel_fn = nn.ReLU(inplace=True), 
                            kernel_epsilon = 0.001, normalize_data = True, device = None):
    b, h, *_ = data.shape
    # q, k, v: (B*N, H, L, E/H)

    data_normalizer = (data.shape[-1] ** -0.25) if normalize_data else 1.

    if projection_matrix is None:
        return kernel_fn(data_normalizer * data) + kernel_epsilon

    data = data_normalizer*data # (B*N, H, L, E/H)
    # (B*N, H, L, E/H) @ (nb_features, E/H)^T -> (B*N, H, L, nb_features)
    data = torch.matmul(data, projection_matrix.T) 
    data = kernel_fn(data) + kernel_epsilon # (B*N, H, L, nb_features)
    return data.type_as(data) # (B*N, H, L, nb_features)

def orthogonal_matrix_chunk(cols, qr_uniform_q = False, device = None):
    unstructured_block = torch.randn((cols, cols), device = device)
    q, r = torch.linalg.qr(unstructured_block.cpu(), 'reduced')
    q, r = map(lambda t: t.to(device), (q, r))
    
    # proposed by @Parskatt
    # to make sure Q is uniform https://arxiv.org/pdf/math-ph/0609050.pdf
    if qr_uniform_q:
        d = torch.diag(r, 0)
        q *= d.sign()
    return q.t()

def gaussian_orthogonal_random_matrix(nb_rows, nb_columns, scaling = 0, qr_uniform_q = False, device = None):
    nb_full_blocks = int(nb_rows / nb_columns)

    block_list = []

    for _ in range(nb_full_blocks):
        q = orthogonal_matrix_chunk(nb_columns, qr_uniform_q = qr_uniform_q, device = device)
        block_list.append(q)

    remaining_rows = nb_rows - nb_full_blocks * nb_columns # 32
    if remaining_rows > 0:
        q = orthogonal_matrix_chunk(nb_columns, qr_uniform_q = qr_uniform_q, device = device)
        block_list.append(q[:remaining_rows])

    final_matrix = torch.cat(block_list)

    if scaling == 0:
        multiplier = torch.randn((nb_rows, nb_columns), device = device).norm(dim = 1)
    elif scaling == 1:
        multiplier = math.sqrt((float(nb_columns))) * torch.ones((nb_rows,), device = device)
    else:
        raise ValueError(f'Invalid scaling {scaling}')

    return torch.diag(multiplier) @ final_matrix

# linear attention classes with softmax kernel

# non-causal linear attention
def linear_attention_raw(q, k, v, linear_attention=None, only_attention=False, normalize=True):
    # q,k: (B*N, H, L, nb_features), v: (B*N, H, L, E/L)
    if not only_attention:
        L = k.shape[-2]
        # (B*N, H, L, nb_features) @ (B*N, H, nb_features) -> (B*N, H, L)
        D_inv = 1. / torch.einsum('...nd,...d->...n', q, k.mean(dim=-2))
        # (B*N, H, L, nb_features) @ (B*N, H, L, E/H) -> (B*N, H, nb_features, E/L)
        context = torch.einsum('...nd,...ne->...de', k/float(L), v)
        del k, v
        # (B*N, H, L) @ (B*N, H, L, nb_features) -> (B*N, H, L, nb_features)
        out = torch.einsum('...n,...nd->...nd', D_inv, q)
        del D_inv, q
        # (B*N, H, L, nb_features) @ (B*N, H, nb_features, E/L) -> (B*N, H, L, E/L)
        out = torch.einsum('...nd,...de->...ne', out, context)
        return out
    else:
        #del v
        L = k.shape[-2]
        if normalize:
            # (B*N, H, L, nb_features) @ (B*N, H, nb_features) -> (B*N, H, L)
            D_inv = 1. / torch.einsum('...nd,...d->...n', q, k.mean(dim=-2))
            # (B*N, H, L) @ (B*N, H, L, nb_features) -> (B*N, H, L, nb_features)
            out = torch.einsum('...n,...nd->...nd', D_inv, q)
            del D_inv, q
            # (B*N, H, L, nb_features) @ (B*N, H, L, nb_features) -> (B*N, H, L, L)
            out = torch.einsum('...ad,...bd->...ab', out, k/float(L))
            del k
        else:
            # (B*N, H, L, nb_features) @ (B*N, H, L, nb_features) -> (B*N, H, L, L)
            out = torch.einsum('...ad,...bd->...ab', q, k/float(L))
        return out

# non-causal linear attention
def linear_attention(q, k, self_attn_padding_mask=None):
    """
    Get the attention matrix
    """
    
    # q,k: (B*N, H, L, nb_features), v: (B*N, H, L, E/L)
    
    L = k.shape[-2]
    # (B*N, H, L, nb_features) @ (B*N, H, nb_features) -> (B*N, H, L)
    D_inv = 1. / torch.einsum('...nd,...d->...n', q, k.mean(dim=-2))
    # (B*N, H, L) @ (B*N, H, L, nb_features) -> (B*N, H, L, nb_features)
    attention = torch.einsum('...n,...nd->...nd', D_inv, q)
    del D_inv, q
    # (B*N, H, L, nb_features) @ (B*N, H, L, nb_features) -> (B*N, H, L, L)
    attention = torch.einsum('...ad,...bd->...ab', attention, k/float(L))
    
    if self_attn_padding_mask is not None:
        # self_attn_padding_mask: [B*R, L]
        # attention = attention * (1 - self_attn_padding_mask[:, None, :, None])
        raise RuntimeError("Not Implement Now...")
    
    return attention # (B*N, H, L, L)

class FastAttention(nn.Module):
    def __init__(self, dim_heads, nb_features = None, ortho_scaling = 0, generalized_attention = True, 
                    kernel_fn = nn.ReLU(inplace=True), qr_uniform_q = False, no_projection = False):
        super().__init__()
        nb_features = default(nb_features, int(dim_heads * math.log(dim_heads)))

        self.dim_heads = dim_heads
        self.nb_features = nb_features
        self.ortho_scaling = ortho_scaling

        if not no_projection:
            self.create_projection = partial(gaussian_orthogonal_random_matrix, nb_rows = self.nb_features, 
                    nb_columns = dim_heads, scaling = ortho_scaling, qr_uniform_q = qr_uniform_q)
            projection_matrix = self.create_projection()
            self.register_buffer('projection_matrix', projection_matrix)

        self.generalized_attention = generalized_attention
        self.kernel_fn = kernel_fn

        # if this is turned on, no projection will be used
        # queries and keys will be softmax-ed as in the original efficient attention paper
        self.no_projection = no_projection


    @torch.no_grad()
    def redraw_projection_matrix(self, device):
        projections = self.create_projection(device = device)
        self.projection_matrix.copy_(projections)
        del projections
    
    def project_qk(self, q, k):
        # q,k,v: (B*N, H, L, E/L)
        device = q.device

        if self.no_projection:
            q = q.softmax(dim = -1)
            k.softmax(dim = -2)

        elif self.generalized_attention:
            create_kernel = partial(generalized_kernel, kernel_fn = self.kernel_fn, projection_matrix = self.projection_matrix, device = device)
            q, k = map(create_kernel, (q, k)) # q,k: (B*N, H, L, nb_features)
        else:
            create_kernel = partial(softmax_kernel, projection_matrix = self.projection_matrix, device = device)
            q = create_kernel(q, is_query = True)
            k = create_kernel(k, is_query = False)
        
        return q, k
    
    def forward(self, q, k, v, attention_mask=None):
        """
        q,k,v:    (B*N, H, L, E/L)
        output:   (B*N, H, L, E/L)
        """
        
        attn = self.get_attention(q, k) # (B*N, H, L, L)
        
        # (B*N, H, L, L) @ (B*N, H, L, E/L) -> (B*N, H, L, E/L)
        output = attn @ v

        return output
    
    def get_attention(self, q, k, normalize=True):
        """
        q,k:    (B*N, H, L, E/L)
        output: (B*N, H, L, L)
        """
        q, k = self.project_qk(q, k)    # (B*N, H, L, E/L)
        attn = linear_attention(q, k)   # (B*N, H, L, L)
        return attn

class SelfAttention(nn.Module):
    def __init__(self, dim, k_dim=None, heads = 8, local_heads = 0, local_window_size = 256, 
            nb_features = None, feature_redraw_interval = 1000, generalized_attention = False, 
            kernel_fn = nn.ReLU(inplace=True), qr_uniform_q = False, dropout = 0., no_projection = False):
        super().__init__()
        assert dim % heads == 0, 'dimension must be divisible by number of heads'
        dim_head = dim // heads
        inner_dim = dim_head * heads

        if k_dim == None:
            k_dim = dim

        self.fast_attention = FastAttention(dim_head, nb_features, generalized_attention = generalized_attention, 
            kernel_fn = kernel_fn, qr_uniform_q = qr_uniform_q, no_projection = no_projection)

        self.heads = heads
        self.dim = dim

        self.to_query = nn.Linear(dim, inner_dim)
        self.to_key   = nn.Linear(k_dim, inner_dim)
        self.to_value = nn.Linear(k_dim, inner_dim)
        self.to_out   = nn.Linear(inner_dim, dim)
        self.dropout  = nn.Dropout(dropout, inplace=True)

        self.feature_redraw_interval = feature_redraw_interval
        self.register_buffer("calls_since_last_redraw", torch.tensor(0))

        self.max_tokens = 2**17 # 2**16
    
    def check_redraw_projections(self):
        if not self.training:
            return

        if exists(self.feature_redraw_interval) and self.calls_since_last_redraw >= self.feature_redraw_interval:
            device = get_module_device(self)

            fast_attentions = find_modules(self, FastAttention)
            for fast_attention in fast_attentions:
                fast_attention.redraw_projection_matrix(device)

            self.calls_since_last_redraw.zero_()
            return

        self.calls_since_last_redraw += 1
    
    def _batched_forward(self, q, k, v):
        b1, h, n1 = q.shape[:3]
        out = torch.empty((b1, h, n1, self.dim//h), dtype=q.dtype, device=q.device)
        shift = self.max_tokens // n1
        for i_b in range(0, b1, shift):
            start = i_b
            end = min(i_b+shift, b1)
            out[start:end] = self.fast_attention(q[start:end], k[start:end], v[start:end])
        return out
         
    #def forward(self, query, key, value, **kwargs):
    def forward(self, input_x, attention_mask=None):
        """
        input_x:  (B*N, L, E)
        output:   (B*N, L, E)
        """
        self.check_redraw_projections()
        
        # query: (B*N, L, E)
        batch_size, length, _, h = *input_x.shape, self.heads

        atten = self.get_attention(input_x, attention_mask, scalling=1)
        
        v = self.to_value(input_x)
        # (B*N, L, H, E/H) -> (B*N, H, L, E/H)
        v = v.reshape(batch_size, length, h, -1).permute(0,2,1,3)
        
        # (B*N, H, L, L) @ (B*N, H, L, E/L) -> (B*N, H, L, E/L)
        out = atten @ v
    
        # out: (B*N, H, L, E/L)
        # (B*N, H, L, E/L) -> (B*N, L, H, E/L) -> (B*N, L, E)
        out = out.permute(0,2,1,3).reshape(batch_size,length,-1)
        out =  self.to_out(out) # (B*N, L, E) -> (B*N, L, E)
        return self.dropout(out) # (B*N, L, E)
    
    def get_attention(self, input_x, attention_mask, scalling):
        """
        input_x:  (B*N, L, E)
        output:   (B*N, H, L, L)
        """
        self.check_redraw_projections()
        
        batch_size, length, _, h = *input_x.shape, self.heads
        
        q = self.to_query(input_x) * scalling
        k = self.to_key(input_x)
        
        q = q.reshape(batch_size, length, h, -1).permute(0,2,1,3) 
        k = k.reshape(batch_size, length, h, -1).permute(0,2,1,3) 
        
        atten = self.fast_attention.get_attention(q, k) 
        
        return atten


class RowSelfAttention(nn.Module):
    """Compute self-attention over rows of a 2D input."""

    def __init__(
        self,
        embed_dim,
        num_heads,
        nb_features=16,
        dropout=0.0,
        tiled=False
    ):
        super().__init__()
        
        assert embed_dim % num_heads == 0
        
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads
        self.tiled = tiled
        # input: (B*R, C, E)
        self.performer = SelfAttention(dim=embed_dim, heads=num_heads, 
                                       nb_features=nb_features, dropout=dropout, 
                                       generalized_attention=True)

    def forward(self, x, self_attn_mask=None, self_attn_padding_mask=None):
        """
        x: [R, C, B, E]
        self_attn_padding_mask: [B, R, C]
        """
        num_rows, num_cols, batch_size, embed_dim = x.size()
        
        if self.tiled:
            scalling = (1/np.sqrt(self.head_dim * num_rows))
            
            # [R, C, B, E] -> [R, B, C, E] -> [R*B, C, E]
            input_rows = x.permute([0, 2, 1, 3]).reshape([num_rows*batch_size, num_cols, embed_dim]) 
            # (R*B, H, C, C)
            attns = self.performer.get_attention(input_rows, None, scalling)
            # (R*B, H, C, C) -> (R, B, H, C, C) -> (B, H, C, C)
            attn_probs = attns.reshape([num_rows, batch_size, self.num_heads, num_cols, num_cols]).mean(0) 
            
            # [R, C, B, E] -> [R, C, B, E] -> [B, E, R, C] -> [B, H, H/E, R, C]
            v = self.performer.to_value(x).permute([2,3,0,1]).reshape([batch_size, self.num_heads, self.head_dim, num_rows, num_cols])
            
            # (B, H, C, C) @ [B, H, H/E, R, C] -> [B, H, H/E, R, C]
            out = torch.einsum('bhij,bhdri->bhdri', attn_probs, v) # attn_probs @ v.transpose(-1,-2)
            
            # [B, H, H/E, R, C] -> [R, C, B, H, H/E] -> [R, C, B, E]
            out = out.permute([3,4,0,1,2]).reshape([num_rows, num_cols, batch_size, embed_dim])
            assert out.size() == x.size()
            
            return out
        else:
            if self_attn_padding_mask is not None:
                self_attn_padding_mask = self_attn_padding_mask.reshape([batch_size*num_rows, num_cols]) # [B*R, C]

            x = x.permute([2,0,1,3]) # [B, R, C, E]
            x = x.reshape([batch_size*num_rows, num_cols, embed_dim])

            y = self.performer(x, self_attn_padding_mask) # [B*R, C, E], [B*R, C] -> [B, L, E]
            y = y.reshape([batch_size, num_rows, num_cols, embed_dim])
            y = y.permute([1,2,0,3]) # [num_rows, num_cols, batch_size, embed_dim]
        
            return y
