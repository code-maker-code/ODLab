# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
DETR Transformer class.

Copy-paste from torch.nn.Transformer with modifications:
    * positional encodings are passed in MHattention
    * extra LN at the end of encoder is removed
    * decoder returns a stack of activations from all decoding layers
"""
import math
import copy
import torch
import torch.nn as nn

from .transformer_encoder import DETRTransformerEncoderLayer, PlainDETRTransformerEncoderLayer
from .transformer_decoder import DETRTransformerDecoderLayer, PlainDETRTransformerDecoderLayer
from ..basic.mlp import MLP


def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])


# ----------------------------- PlainDETR Transformer -----------------------------
class PlainDETRTransformer(nn.Module):
    def __init__(self,
                 is_train            :bool  = False,
                 d_model             :int   = 512,
                 # Encoder
                 num_encoder         :int   = 6,
                 encoder_num_head    :int   = 8,
                 encoder_mlp_ratio   :float = 4.0,
                 encoder_dropout     :float = 0.1,
                 encoder_act_type    :str   = "relu",
                 upsample            :bool  = False,
                 upsample_first      :bool  = False,
                 # Decoder
                 num_decoder         :int   = 6,
                 decoder_num_head    :int   = 8,
                 decoder_mlp_ratio   :float = 4.0,
                 decoder_dropout     :float = 0.1,
                 decoder_act_type    :str   = "relu",
                 # Other
                 num_classes          :int   = 80,
                 num_queries_one2one  :int   = 300,
                 num_queries_one2many :int   = 1500,
                 norm_before          :bool  = False,
                 return_intermediate  :bool  = False):
        super().__init__()
        # --------------- Basic parameters ---------------
        self.is_train = is_train
        self.d_model = d_model
        self.upsample = upsample
        self.upsample_first = upsample_first
        self.num_queries_one2one = num_queries_one2one
        self.num_queries_one2many = num_queries_one2many
        self.num_queries = num_queries_one2one + num_queries_one2many if is_train else num_queries_one2one
        self.num_classes = num_classes
        self.return_intermediate = return_intermediate
        # --------------- Network parameters ---------------
        ## Transformer Encoder
        self.encoder_layers = None
        if num_encoder > 0:
            encoder_layer = PlainDETRTransformerEncoderLayer(d_model, encoder_num_head, encoder_mlp_ratio, encoder_dropout, encoder_act_type)
            self.encoder_layers = _get_clones(encoder_layer, num_encoder)

        ## Upsample layer
        self.upsample_layer = None
        if upsample:
            self.upsample_layer = nn.Sequential(
                nn.ConvTranspose2d(d_model, d_model, kernel_size=4, padding=1, stride=2),
                nn.GroupNorm(32, d_model)
            )

        ## Transformer Decoder
        self.decoder_layers = None
        if num_decoder > 0:
            decoder_layer = PlainDETRTransformerDecoderLayer(d_model, decoder_num_head, decoder_mlp_ratio, decoder_dropout, decoder_act_type)
            self.decoder_layers = _get_clones(decoder_layer, num_decoder)

        ## Adaptive pos_embed
        self.ref_point_head = MLP(2 * d_model, d_model, d_model, 2)

        ## Object Queries
        self.query_embed = nn.Embedding(self.num_queries, d_model)
        self.refpoint_embed = nn.Embedding(self.num_queries, 4)
        
        ## Output head
        class_embed = nn.Linear(self.d_model, num_classes)
        bbox_embed  = MLP(self.d_model, self.d_model, 4, 3)
        self.class_embed = nn.ModuleList([copy.deepcopy(class_embed) for _ in range(num_decoder)])
        self.bbox_embed  = nn.ModuleList([copy.deepcopy(bbox_embed)  for _ in range(num_decoder)])

        self.init_weight()

    # -------------- Basic functions --------------
    def init_weight(self):
        # init class embed bias
        init_prob = 0.01
        bias_value = -torch.log(torch.tensor((1. - init_prob) / init_prob))
        for class_embed in self.class_embed:
            class_embed.bias.data = torch.ones(self.num_classes) * bias_value
        # init bbox embed bias
        for bbox_embed in self.bbox_embed:
            nn.init.constant_(bbox_embed.layers[-1].weight.data, 0)
            nn.init.constant_(bbox_embed.layers[-1].bias.data, 0)
        # init weights
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def pos2posembed(self, pos, temperature=10000):
        scale = 2 * math.pi
        num_pos_feats = self.d_model // 2
        pos = pos * scale
        dim_t = torch.arange(num_pos_feats, dtype=torch.float32, device=pos.device)
        dim_t_ = torch.div(dim_t, 2, rounding_mode='floor') / num_pos_feats
        dim_t = temperature ** (2 * dim_t_)
        pos_x = pos[..., 0, None] / dim_t
        pos_y = pos[..., 1, None] / dim_t
        pos_x = torch.stack((pos_x[..., 0::2].sin(), pos_x[..., 1::2].cos()), dim=-1).flatten(-2)
        pos_y = torch.stack((pos_y[..., 0::2].sin(), pos_y[..., 1::2].cos()), dim=-1).flatten(-2)
        
        if pos.size(-1) == 2:    
            posemb = torch.cat((pos_y, pos_x), dim=-1)
        elif pos.size(-1) == 4:
            w_embed = pos[:, :, 2] * scale
            pos_w = w_embed[:, :, None] / dim_t
            pos_w = torch.stack((pos_w[:, :, 0::2].sin(), pos_w[:, :, 1::2].cos()), dim=3).flatten(2)
            h_embed = pos[:, :, 3] * scale
            pos_h = h_embed[:, :, None] / dim_t
            pos_h = torch.stack((pos_h[:, :, 0::2].sin(), pos_h[:, :, 1::2].cos()), dim=3).flatten(2)
            posemb = torch.cat((pos_y, pos_x, pos_w, pos_h), dim=-1)
        else:
            raise ValueError("Unknown pos_tensor shape(-1):{}".format(pos.size(-1)))
        
        return posemb

    def get_posembed(self, mask, temperature=10000):
        scale = 2 * math.pi
        not_mask = ~mask

        # [B, H, W]
        y_embed = not_mask.cumsum(1, dtype=torch.float32)
        x_embed = not_mask.cumsum(2, dtype=torch.float32)

        y_embed = (y_embed - 0.5) / (y_embed[:, -1:, :] + 1e-6)* scale
        x_embed = (x_embed - 0.5) / (x_embed[:, :, -1:] + 1e-6)* scale
    
        # [H, W] -> [B, H, W, 2]
        pos = torch.stack([x_embed, y_embed], dim=-1)

        # [B, H, W, C]
        pos_embed = self.pos2posembed(pos, temperature)
        pos_embed = pos_embed.permute(0, 3, 1, 2)
        
        return pos_embed        

    def inverse_sigmoid(self, x):
        x = x.clamp(min=0, max=1)
        return torch.log(x.clamp(min=1e-5)/(1 - x).clamp(min=1e-5))

    def resize_mask(self, src, mask=None):
        bs, c, h, w = src.shape
        if mask is not None:
            # [B, H, W]
            mask = nn.functional.interpolate(mask[None].float(), size=[h, w]).bool()[0]
        else:
            mask = torch.zeros([bs, h, w], device=src.device, dtype=torch.bool)

        return mask

    @torch.jit.unused
    def set_aux_loss(self, outputs_class, outputs_coord):
        # this is a workaround to make torchscript happy, as torchscript
        # doesn't support dictionary with non-homogeneous values, such
        # as a dict having both a Tensor and a list.
        return [{'pred_logits': a, 'pred_boxes': b}
                for a, b in zip(outputs_class[:-1], outputs_coord[:-1])]

    # -------------- Model forward --------------
    def forward_pre_upsample(self, src, src_mask=None):
        ## Upsample feature
        if self.upsample_layer:
            src = self.upsample_layer(src)
        bs, c, h, w = src.shape
        mask = self.resize_mask(src, src_mask)

        # ------------------------ Transformer Encoder ------------------------
        ## Get pos_embed: [B, C, H, W]
        pos_embed = self.get_posembed(mask)
        ## Reshape: [B, C, H, W] -> [N, B, C], N = HW
        src = src.flatten(2).permute(2, 0, 1)
        pos_embed = pos_embed.flatten(2).permute(2, 0, 1)
        mask = mask.flatten(1)
        ## Encoder layer
        if self.encoder_layers:
            for encoder_layer in self.encoder_layers:
                src = encoder_layer(src,
                                    src_key_padding_mask = mask,
                                    pos_embed            = pos_embed)

        # ------------------------ Transformer Decoder ------------------------
        ## Prepare queries
        tgt = self.query_embed.weight
        query_embed = self.refpoint_embed.weight
        tgt = tgt[:, None, :].repeat(1, bs, 1)
        query_embed = query_embed[:, None, :].repeat(1, bs, 1)
        
        ref_point = query_embed.sigmoid()
        ref_points = [ref_point]
        
        ## Prepare attn mask
        self_attn_mask = None
        use_one2many = self.num_queries_one2many > 0 and self.is_train
        if use_one2many:
            self_attn_mask = torch.zeros([self.num_queries, self.num_queries]).bool().to(src.device)
            self_attn_mask[self.num_queries_one2one:, :self.num_queries_one2one] = True
            self_attn_mask[:self.num_queries_one2one, self.num_queries_one2one:] = True

        ## Decoder layer
        output = tgt
        outputs = []
        output_classes_one2one = []
        output_coords_one2one = []
        output_classes_one2many = []
        output_coords_one2many = []
        for layer_id, decoder_layer in enumerate(self.decoder_layers):
            # Conditional query
            query_sine_embed = self.pos2posembed(ref_point)
            query_pos = self.ref_point_head(query_sine_embed)

            # Decoder
            output = decoder_layer(output,
                                   src,
                                   tgt_mask                = self_attn_mask,
                                   memory_key_padding_mask = mask,
                                   pos                     = pos_embed,
                                   query_pos               = query_pos
                                   )
            
            # Look forward twice
            tmp = self.bbox_embed[layer_id](output)
            new_ref_point = tmp + self.inverse_sigmoid(ref_point)
            new_ref_point = new_ref_point.sigmoid()
            ref_point = new_ref_point.detach()

            outputs.append(output)
            ref_points.append(ref_point)

        # ------------------------ Detection Head ------------------------
        for lid, (ref_sig, output) in enumerate(zip(ref_points[:-1], outputs)):
            ## class pred
            output_class = self.class_embed[lid](output)
            ## bbox pred
            tmp = self.bbox_embed[lid](output)
            tmp += self.inverse_sigmoid(ref_sig)
            output_coord = tmp.sigmoid()

            output_classes_one2one.append(output_class[:self.num_queries_one2one])
            output_coords_one2one.append(output_coord[:self.num_queries_one2one])
            if use_one2many:
                output_classes_one2many.append(output_class[self.num_queries_one2many:])
                output_coords_one2many.append(output_coord[self.num_queries_one2many:])

        # [L, Nq, B, Nc] -> [L, B, Nq, Nc]
        output_classes_one2one = torch.stack(output_classes_one2one).permute(0, 2, 1, 3)
        output_coords_one2one  = torch.stack(output_coords_one2one).permute(0, 2, 1, 3)
        if use_one2many:
            output_classes_one2many = torch.stack(output_classes_one2many).permute(0, 2, 1, 3)
            output_coords_one2many  = torch.stack(output_coords_one2many).permute(0, 2, 1, 3)

        # --------------------- Re-organize outputs ---------------------
        ## One2one outputs
        outputs = {
            "pred_logits": output_classes_one2one[-1],
            "pred_boxes":  output_coords_one2one[-1]
        }
        if self.return_intermediate:
            outputs['aux_outputs'] = self.set_aux_loss(output_classes_one2one, output_coords_one2one)
        ## One2many outputs
        if use_one2many:
            outputs["pred_logits_one2many"] = output_classes_one2many[-1]
            outputs["pred_boxes_one2many"] = output_coords_one2many[-1]
            if self.return_intermediate:
                outputs['aux_outputs_one2many'] = self.set_aux_loss(output_classes_one2many, output_coords_one2many)

        return outputs

    def forward_post_upsample(self, src, src_mask=None):
        bs, c, h, w = src.shape
        mask = self.resize_mask(src, src_mask)

        # ------------------------ Transformer Encoder ------------------------
        ## Get pos_embed: [B, C, H, W]
        pos_embed = self.get_posembed(mask)
        ## Reshape: [B, C, H, W] -> [N, B, C], N = HW
        src = src.flatten(2).permute(2, 0, 1)
        pos_embed = pos_embed.flatten(2).permute(2, 0, 1)
        mask = mask.flatten(1)
        ## Encoder layer
        if self.encoder_layers:
            for encoder_layer in self.encoder_layers:
                src = encoder_layer(src,
                                    src_key_padding_mask = mask,
                                    pos_embed            = pos_embed)

        ## Upsample feature
        if self.upsample_layer:
            # Reshape: [N, B, C] -> [B, C, H, W]
            src = src.permute(1, 2, 0).reshape(bs, c, h, w)
            src = self.upsample_layer(src)
            mask = self.resize_mask(src, src_mask)
            # Generate pos_embed for upsampled src
            pos_embed = self.get_posembed(mask)
            # Reshape: [B, C, H, W] -> [N, B, C], N = HW
            src = src.flatten(2).permute(2, 0, 1)
            pos_embed = pos_embed.flatten(2).permute(2, 0, 1)
            mask = mask.flatten(1)

        # ------------------------ Transformer Decoder ------------------------
        ## Prepare queries
        tgt = self.query_embed.weight
        query_embed = self.refpoint_embed.weight
        tgt = tgt[:, None, :].repeat(1, bs, 1)
        query_embed = query_embed[:, None, :].repeat(1, bs, 1)
        
        ref_point = query_embed.sigmoid()
        ref_points = [ref_point]
        
        ## Prepare attn mask
        self_attn_mask = None
        use_one2many = self.num_queries_one2many > 0 and self.is_train
        if use_one2many:
            self_attn_mask = torch.zeros([self.num_queries, self.num_queries]).bool().to(src.device)
            self_attn_mask[self.num_queries_one2one:, :self.num_queries_one2one] = True
            self_attn_mask[:self.num_queries_one2one, self.num_queries_one2one:] = True

        ## Decoder layer
        output = tgt
        outputs = []
        output_classes_one2one = []
        output_coords_one2one = []
        output_classes_one2many = []
        output_coords_one2many = []
        for layer_id, decoder_layer in enumerate(self.decoder_layers):
            # Conditional query
            query_sine_embed = self.pos2posembed(ref_point)
            query_pos = self.ref_point_head(query_sine_embed)

            # Decoder
            output = decoder_layer(output,
                                   src,
                                   tgt_mask                = self_attn_mask,
                                   memory_key_padding_mask = mask,
                                   pos                     = pos_embed,
                                   query_pos               = query_pos
                                   )
            
            # Iter update
            tmp = self.bbox_embed[layer_id](output)
            new_ref_point = tmp + self.inverse_sigmoid(ref_point)
            new_ref_point = new_ref_point.sigmoid()
            ref_point = new_ref_point.detach()

            outputs.append(output)
            ref_points.append(ref_point)

        # ------------------------ Detection Head ------------------------
        for lid, (ref_sig, output) in enumerate(zip(ref_points[:-1], outputs)):
            ## class pred
            output_class = self.class_embed[lid](output)
            ## bbox pred
            tmp = self.bbox_embed[lid](output)
            tmp += self.inverse_sigmoid(ref_sig)
            output_coord = tmp.sigmoid()

            output_classes_one2one.append(output_class[:self.num_queries_one2one])
            output_coords_one2one.append(output_coord[:self.num_queries_one2one])
            if use_one2many:
                output_classes_one2many.append(output_class[self.num_queries_one2one:])
                output_coords_one2many.append(output_coord[self.num_queries_one2one:])

        # [L, Nq, B, Nc] -> [L, B, Nq, Nc]
        output_classes_one2one = torch.stack(output_classes_one2one).permute(0, 2, 1, 3)
        output_coords_one2one  = torch.stack(output_coords_one2one).permute(0, 2, 1, 3)
        if use_one2many:
            output_classes_one2many = torch.stack(output_classes_one2many).permute(0, 2, 1, 3)
            output_coords_one2many  = torch.stack(output_coords_one2many).permute(0, 2, 1, 3)

        # --------------------- Re-organize outputs ---------------------
        ## One2one outputs
        outputs = {
            "pred_logits": output_classes_one2one[-1],
            "pred_boxes":  output_coords_one2one[-1]
        }
        if self.return_intermediate:
            outputs['aux_outputs'] = self.set_aux_loss(output_classes_one2one, output_coords_one2one)
        ## One2many outputs
        if use_one2many:
            outputs["pred_logits_one2many"] = output_classes_one2many[-1]
            outputs["pred_boxes_one2many"] = output_coords_one2many[-1]
            if self.return_intermediate:
                outputs['aux_outputs_one2many'] = self.set_aux_loss(output_classes_one2many, output_coords_one2many)

        return outputs

    def forward(self, src, src_mask=None):
        if self.upsample_first:
            return self.forward_pre_upsample(src, src_mask)
        else:
            return self.forward_post_upsample(src, src_mask)


class BackupPlainDETRTransformer(nn.Module):
    def __init__(self,
                 is_train            :bool  = False,
                 d_model             :int   = 512,
                 # Encoder
                 num_encoder         :int   = 6,
                 encoder_num_head    :int   = 8,
                 encoder_mlp_ratio   :float = 4.0,
                 encoder_dropout     :float = 0.1,
                 encoder_act_type    :str   = "relu",
                 upsample            :bool  = False,
                 upsample_first      :bool  = False,
                 # Decoder
                 num_decoder         :int   = 6,
                 decoder_num_head    :int   = 8,
                 decoder_mlp_ratio   :float = 4.0,
                 decoder_dropout     :float = 0.1,
                 decoder_act_type    :str   = "relu",
                 # Other
                 num_classes          :int   = 80,
                 num_queries_one2one  :int   = 300,
                 num_queries_one2many :int   = 1500,
                 norm_before          :bool  = False,
                 return_intermediate  :bool  = False):
        super().__init__()
        # --------------- Basic parameters ---------------
        self.is_train = is_train
        self.d_model = d_model
        self.upsample = upsample
        self.upsample_first = upsample_first
        self.num_queries_one2one = num_queries_one2one
        self.num_queries_one2many = num_queries_one2many
        self.num_queries = num_queries_one2one + num_queries_one2many if is_train else num_queries_one2one
        self.num_classes = num_classes
        self.return_intermediate = return_intermediate
        # --------------- Network parameters ---------------
        ## Transformer Encoder
        self.encoder_layers = None
        if num_encoder > 0:
            encoder_layer = PlainDETRTransformerEncoderLayer(d_model, encoder_num_head, encoder_mlp_ratio, encoder_dropout, encoder_act_type)
            self.encoder_layers = _get_clones(encoder_layer, num_encoder)

        ## Upsample layer
        self.upsample_layer = None
        if upsample:
            self.upsample_layer = nn.Sequential(
                nn.ConvTranspose2d(d_model, d_model, kernel_size=4, padding=1, stride=2),
                nn.GroupNorm(32, d_model)
            )

        ## Transformer Decoder
        self.decoder_layers = None
        if num_decoder > 0:
            decoder_layer = PlainDETRTransformerDecoderLayer(d_model, decoder_num_head, decoder_mlp_ratio, decoder_dropout, decoder_act_type)
            self.decoder_layers = _get_clones(decoder_layer, num_decoder)

        ## Adaptive pos_embed
        self.ref_point_head = MLP(2 * d_model, d_model, d_model, 2)

        ## Object Queries
        self.query_embed = nn.Embedding(self.num_queries, d_model)
        self.refpoint_embed = nn.Embedding(self.num_queries, 4)
        
        ## Output head
        class_embed = nn.Linear(self.d_model, num_classes)
        bbox_embed  = MLP(self.d_model, self.d_model, 4, 3)
        self.class_embed = nn.ModuleList([copy.deepcopy(class_embed) for _ in range(num_decoder)])
        self.bbox_embed  = nn.ModuleList([copy.deepcopy(bbox_embed)  for _ in range(num_decoder)])

        self.init_weight()

    # -------------- Basic functions --------------
    def init_weight(self):
        # init class embed bias
        init_prob = 0.01
        bias_value = -torch.log(torch.tensor((1. - init_prob) / init_prob))
        for class_embed in self.class_embed:
            class_embed.bias.data = torch.ones(self.num_classes) * bias_value
        # init bbox embed bias
        for bbox_embed in self.bbox_embed:
            nn.init.constant_(bbox_embed.layers[-1].weight.data, 0)
            nn.init.constant_(bbox_embed.layers[-1].bias.data, 0)
        # init weights
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def pos2posembed(self, pos, temperature=10000):
        scale = 2 * math.pi
        num_pos_feats = self.d_model // 2
        pos = pos * scale
        dim_t = torch.arange(num_pos_feats, dtype=torch.float32, device=pos.device)
        dim_t_ = torch.div(dim_t, 2, rounding_mode='floor') / num_pos_feats
        dim_t = temperature ** (2 * dim_t_)
        pos_x = pos[..., 0, None] / dim_t
        pos_y = pos[..., 1, None] / dim_t
        pos_x = torch.stack((pos_x[..., 0::2].sin(), pos_x[..., 1::2].cos()), dim=-1).flatten(-2)
        pos_y = torch.stack((pos_y[..., 0::2].sin(), pos_y[..., 1::2].cos()), dim=-1).flatten(-2)
        
        if pos.size(-1) == 2:    
            posemb = torch.cat((pos_y, pos_x), dim=-1)
        elif pos.size(-1) == 4:
            w_embed = pos[:, :, 2] * scale
            pos_w = w_embed[:, :, None] / dim_t
            pos_w = torch.stack((pos_w[:, :, 0::2].sin(), pos_w[:, :, 1::2].cos()), dim=3).flatten(2)
            h_embed = pos[:, :, 3] * scale
            pos_h = h_embed[:, :, None] / dim_t
            pos_h = torch.stack((pos_h[:, :, 0::2].sin(), pos_h[:, :, 1::2].cos()), dim=3).flatten(2)
            posemb = torch.cat((pos_y, pos_x, pos_w, pos_h), dim=-1)
        else:
            raise ValueError("Unknown pos_tensor shape(-1):{}".format(pos.size(-1)))
        
        return posemb

    def get_posembed(self, mask, temperature=10000):
        scale = 2 * math.pi
        not_mask = ~mask

        # [B, H, W]
        y_embed = not_mask.cumsum(1, dtype=torch.float32)
        x_embed = not_mask.cumsum(2, dtype=torch.float32)

        y_embed = (y_embed - 0.5) / (y_embed[:, -1:, :] + 1e-6)* scale
        x_embed = (x_embed - 0.5) / (x_embed[:, :, -1:] + 1e-6)* scale
    
        # [H, W] -> [B, H, W, 2]
        pos = torch.stack([x_embed, y_embed], dim=-1)

        # [B, H, W, C]
        pos_embed = self.pos2posembed(pos, temperature)
        pos_embed = pos_embed.permute(0, 3, 1, 2)
        
        return pos_embed        

    def inverse_sigmoid(self, x):
        x = x.clamp(min=0, max=1)
        return torch.log(x.clamp(min=1e-5)/(1 - x).clamp(min=1e-5))

    def resize_mask(self, src, mask=None):
        bs, c, h, w = src.shape
        if mask is not None:
            # [B, H, W]
            mask = nn.functional.interpolate(mask[None].float(), size=[h, w]).bool()[0]
        else:
            mask = torch.zeros([bs, h, w], device=src.device, dtype=torch.bool)

        return mask

    @torch.jit.unused
    def set_aux_loss(self, outputs_class, outputs_coord):
        # this is a workaround to make torchscript happy, as torchscript
        # doesn't support dictionary with non-homogeneous values, such
        # as a dict having both a Tensor and a list.
        return [{'pred_logits': a, 'pred_boxes': b}
                for a, b in zip(outputs_class[:-1], outputs_coord[:-1])]

    # -------------- Model forward --------------
    def forward_pre_upsample(self, src, src_mask=None):
        ## Upsample feature
        if self.upsample_layer:
            src = self.upsample_layer(src)
        bs, c, h, w = src.shape
        mask = self.resize_mask(src, src_mask)

        # ------------------------ Transformer Encoder ------------------------
        ## Get pos_embed: [B, C, H, W]
        pos_embed = self.get_posembed(mask)
        ## Reshape: [B, C, H, W] -> [N, B, C], N = HW
        src = src.flatten(2).permute(2, 0, 1)
        pos_embed = pos_embed.flatten(2).permute(2, 0, 1)
        mask = mask.flatten(1)
        ## Encoder layer
        if self.encoder_layers:
            for encoder_layer in self.encoder_layers:
                src = encoder_layer(src,
                                    src_key_padding_mask = mask,
                                    pos_embed            = pos_embed)

        # ------------------------ Transformer Decoder ------------------------
        ## Prepare queries
        tgt = self.query_embed.weight
        query_embed = self.refpoint_embed.weight
        tgt = tgt[:, None, :].repeat(1, bs, 1)
        query_embed = query_embed[:, None, :].repeat(1, bs, 1)

        ## Initialize object queries based on image content
        
        
        ref_point = query_embed.sigmoid()
        ref_points = [ref_point]
        
        ## Prepare attn mask
        self_attn_mask = None
        use_one2many = self.num_queries_one2many > 0 and self.is_train
        if use_one2many:
            self_attn_mask = torch.zeros([self.num_queries, self.num_queries]).bool().to(src.device)
            self_attn_mask[self.num_queries_one2one:, :self.num_queries_one2one] = True
            self_attn_mask[:self.num_queries_one2one, self.num_queries_one2one:] = True

        ## Decoder layer
        output = tgt
        outputs = []
        output_classes_one2one = []
        output_coords_one2one = []
        output_classes_one2many = []
        output_coords_one2many = []
        for layer_id, decoder_layer in enumerate(self.decoder_layers):
            # Conditional query
            query_sine_embed = self.pos2posembed(ref_point)
            query_pos = self.ref_point_head(query_sine_embed)

            # Decoder
            output = decoder_layer(output,
                                   src,
                                   tgt_mask                = self_attn_mask,
                                   memory_key_padding_mask = mask,
                                   pos                     = pos_embed,
                                   query_pos               = query_pos
                                   )
            
            # Look forward twice
            tmp = self.bbox_embed[layer_id](output)
            new_ref_point = tmp + self.inverse_sigmoid(ref_point)
            new_ref_point = new_ref_point.sigmoid()
            ref_point = new_ref_point.detach()

            outputs.append(output)
            ref_points.append(ref_point)

        # ------------------------ Detection Head ------------------------
        for lid, (ref_sig, output) in enumerate(zip(ref_points[:-1], outputs)):
            ## class pred
            output_class = self.class_embed[lid](output)
            ## bbox pred
            tmp = self.bbox_embed[lid](output)
            tmp += self.inverse_sigmoid(ref_sig)
            output_coord = tmp.sigmoid()

            output_classes_one2one.append(output_class[:self.num_queries_one2one])
            output_coords_one2one.append(output_coord[:self.num_queries_one2one])
            if use_one2many:
                output_classes_one2many.append(output_class[self.num_queries_one2one:])
                output_coords_one2many.append(output_coord[self.num_queries_one2one:])

        # [L, Nq, B, Nc] -> [L, B, Nq, Nc]
        output_classes_one2one = torch.stack(output_classes_one2one).permute(0, 2, 1, 3)
        output_coords_one2one  = torch.stack(output_coords_one2one).permute(0, 2, 1, 3)
        if use_one2many:
            output_classes_one2many = torch.stack(output_classes_one2many).permute(0, 2, 1, 3)
            output_coords_one2many  = torch.stack(output_coords_one2many).permute(0, 2, 1, 3)

        # --------------------- Re-organize outputs ---------------------
        ## One2one outputs
        outputs = {
            "pred_logits": output_classes_one2one[-1],
            "pred_boxes":  output_coords_one2one[-1]
        }
        if self.return_intermediate:
            outputs['aux_outputs'] = self.set_aux_loss(output_classes_one2one, output_coords_one2one)
        ## One2many outputs
        if use_one2many:
            outputs["pred_logits_one2many"] = output_classes_one2many[-1]
            outputs["pred_boxes_one2many"] = output_coords_one2many[-1]
            if self.return_intermediate:
                outputs['aux_outputs_one2many'] = self.set_aux_loss(output_classes_one2many, output_coords_one2many)

        return outputs

    def forward_post_upsample(self, src, src_mask=None):
        bs, c, h, w = src.shape
        mask = self.resize_mask(src, src_mask)

        # ------------------------ Transformer Encoder ------------------------
        ## Get pos_embed: [B, C, H, W]
        pos_embed = self.get_posembed(mask)
        ## Reshape: [B, C, H, W] -> [N, B, C], N = HW
        src = src.flatten(2).permute(2, 0, 1)
        pos_embed = pos_embed.flatten(2).permute(2, 0, 1)
        mask = mask.flatten(1)
        ## Encoder layer
        if self.encoder_layers:
            for encoder_layer in self.encoder_layers:
                src = encoder_layer(src,
                                    src_key_padding_mask = mask,
                                    pos_embed            = pos_embed)

        ## Upsample feature
        if self.upsample_layer:
            # Reshape: [N, B, C] -> [B, C, H, W]
            src = src.permute(1, 2, 0).reshape(bs, c, h, w)
            src = self.upsample_layer(src)
            mask = self.resize_mask(src, src_mask)
            # Generate pos_embed for upsampled src
            pos_embed = self.get_posembed(mask)
            # Reshape: [B, C, H, W] -> [N, B, C], N = HW
            src = src.flatten(2).permute(2, 0, 1)
            pos_embed = pos_embed.flatten(2).permute(2, 0, 1)
            mask = mask.flatten(1)

        # ------------------------ Transformer Decoder ------------------------
        ## Prepare queries
        tgt = self.query_embed.weight
        query_embed = self.refpoint_embed.weight
        tgt = tgt[:, None, :].repeat(1, bs, 1)
        query_embed = query_embed[:, None, :].repeat(1, bs, 1)
        
        ref_point = query_embed.sigmoid()
        ref_points = [ref_point]
        
        ## Prepare attn mask
        self_attn_mask = None
        use_one2many = self.num_queries_one2many > 0 and self.is_train
        if use_one2many:
            self_attn_mask = torch.zeros([self.num_queries, self.num_queries]).bool().to(src.device)
            self_attn_mask[self.num_queries_one2one:, :self.num_queries_one2one] = True
            self_attn_mask[:self.num_queries_one2one, self.num_queries_one2one:] = True

        ## Decoder layer
        output = tgt
        outputs = []
        output_classes_one2one = []
        output_coords_one2one = []
        output_classes_one2many = []
        output_coords_one2many = []
        for layer_id, decoder_layer in enumerate(self.decoder_layers):
            # Conditional query
            query_sine_embed = self.pos2posembed(ref_point)
            query_pos = self.ref_point_head(query_sine_embed)

            # Decoder
            output = decoder_layer(output,
                                   src,
                                   tgt_mask                = self_attn_mask,
                                   memory_key_padding_mask = mask,
                                   pos                     = pos_embed,
                                   query_pos               = query_pos
                                   )
            
            # Iter update
            tmp = self.bbox_embed[layer_id](output)
            new_ref_point = tmp + self.inverse_sigmoid(ref_point)
            new_ref_point = new_ref_point.sigmoid()
            ref_point = new_ref_point.detach()

            outputs.append(output)
            ref_points.append(ref_point)

        # ------------------------ Detection Head ------------------------
        for lid, (ref_sig, output) in enumerate(zip(ref_points[:-1], outputs)):
            ## class pred
            output_class = self.class_embed[lid](output)
            ## bbox pred
            tmp = self.bbox_embed[lid](output)
            tmp += self.inverse_sigmoid(ref_sig)
            output_coord = tmp.sigmoid()

            output_classes_one2one.append(output_class[:self.num_queries_one2one])
            output_coords_one2one.append(output_coord[:self.num_queries_one2one])
            if use_one2many:
                output_classes_one2many.append(output_class[self.num_queries_one2one:])
                output_coords_one2many.append(output_coord[self.num_queries_one2one:])

        # [L, Nq, B, Nc] -> [L, B, Nq, Nc]
        output_classes_one2one = torch.stack(output_classes_one2one).permute(0, 2, 1, 3)
        output_coords_one2one  = torch.stack(output_coords_one2one).permute(0, 2, 1, 3)
        if use_one2many:
            output_classes_one2many = torch.stack(output_classes_one2many).permute(0, 2, 1, 3)
            output_coords_one2many  = torch.stack(output_coords_one2many).permute(0, 2, 1, 3)

        # --------------------- Re-organize outputs ---------------------
        ## One2one outputs
        outputs = {
            "pred_logits": output_classes_one2one[-1],
            "pred_boxes":  output_coords_one2one[-1]
        }
        if self.return_intermediate:
            outputs['aux_outputs'] = self.set_aux_loss(output_classes_one2one, output_coords_one2one)
        ## One2many outputs
        if use_one2many:
            outputs["pred_logits_one2many"] = output_classes_one2many[-1]
            outputs["pred_boxes_one2many"] = output_coords_one2many[-1]
            if self.return_intermediate:
                outputs['aux_outputs_one2many'] = self.set_aux_loss(output_classes_one2many, output_coords_one2many)

        return outputs

    def forward(self, src, src_mask=None):
        if self.upsample_first:
            return self.forward_pre_upsample(src, src_mask)
        else:
            return self.forward_post_upsample(src, src_mask)
