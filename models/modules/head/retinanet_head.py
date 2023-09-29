import math
import torch
import torch.nn as nn
from ..conv_modules import Conv


class RetinaNetHead(nn.Module):
    def __init__(self, cfg, in_dim, out_dim, num_classes, num_cls_head=1, num_reg_head=1, act_type='relu', norm_type='BN'):
        super().__init__()
        self.fmp_size = None
        self.DEFAULT_EXP_CLAMP = math.log(1e8)
        self.DEFAULT_SCALE_CLAMP = math.log(1000.0 / 16)
        # ------------------ Basic parameters -------------------
        self.cfg = cfg
        self.in_dim = in_dim
        self.num_classes = num_classes
        self.num_cls_head=num_cls_head
        self.num_reg_head=num_reg_head
        self.act_type=act_type
        self.norm_type=norm_type
        self.stride = cfg['out_stride']
        # ------------------ Anchor parameters -------------------
        self.anchor_size = self.generate_anchor_sizes(cfg)  # [S, KA, 2]
        self.num_anchors = self.anchor_size.shape[1]

        # ------------------ Network parameters -------------------
        ## cls head
        cls_heads = []
        self.cls_head_dim = out_dim
        for i in range(cfg['num_cls_heads']):
            if i == 0:
                cls_heads.append(
                    Conv(in_dim, self.cls_head_dim, k=3, p=1, s=1, 
                        act_type=self.act_type,
                        norm_type=self.norm_type,
                        depthwise=cfg['head_depthwise'])
                        )
            else:
                cls_heads.append(
                    Conv(self.cls_head_dim, self.cls_head_dim, k=3, p=1, s=1, 
                        act_type=self.act_type,
                        norm_type=self.norm_type,
                        depthwise=cfg['head_depthwise'])
                        )
        
        ## reg head
        reg_heads = []
        self.reg_head_dim = out_dim
        for i in range(cfg['num_reg_heads']):
            if i == 0:
                reg_heads.append(
                    Conv(in_dim, self.reg_head_dim, k=3, p=1, s=1, 
                        act_type=self.act_type,
                        norm_type=self.norm_type,
                        depthwise=cfg['head_depthwise'])
                        )
            else:
                reg_heads.append(
                    Conv(self.reg_head_dim, self.reg_head_dim, k=3, p=1, s=1, 
                        act_type=self.act_type,
                        norm_type=self.norm_type,
                        depthwise=cfg['head_depthwise'])
                        )
        self.cls_heads = nn.Sequential(*cls_heads)
        self.reg_heads = nn.Sequential(*reg_heads)

        ## pred layers
        self.cls_pred = nn.Conv2d(self.cls_head_dim, num_classes, kernel_size=3, padding=1)
        self.reg_pred = nn.Conv2d(self.reg_head_dim, 4 * self.num_anchors, kernel_size=3, padding=1)

        # init bias
        self._init_pred_layers()

    def _init_pred_layers(self):  
        # init cls pred
        nn.init.normal_(self.cls_pred.weight, mean=0, std=0.01)
        init_prob = 0.01
        bias_value = -torch.log(torch.tensor((1. - init_prob) / init_prob))
        nn.init.constant_(self.cls_pred.bias, bias_value)
        # init reg pred
        nn.init.normal_(self.reg_pred.weight, mean=0, std=0.01)
        nn.init.constant_(self.reg_pred.bias, 0.0)

    def generate_anchor_sizes(self, cfg):
        basic_anchor_size =   cfg['anchor_config']['basic_size']
        anchor_aspect_ratio = cfg['anchor_config']['aspect_ratio']
        anchor_area_scale =   cfg['anchor_config']['area_scale']

        num_scales = len(basic_anchor_size)
        num_anchors = len(anchor_aspect_ratio) * len(anchor_area_scale)
        anchor_sizes = []
        for size in basic_anchor_size:
            for ar in anchor_aspect_ratio:
                for s in anchor_area_scale:
                    ah, aw = size
                    area = ah * aw * s
                    anchor_sizes.append(
                        [torch.sqrt(torch.tensor(ar * area)),
                         torch.sqrt(torch.tensor(area / ar))]
                         )
        # [S * KA, 2] -> [S, KA, 2]
        anchor_sizes = torch.as_tensor(anchor_sizes).view(num_scales, num_anchors, 2)

        return anchor_sizes

    def generate_anchors(self, level, fmp_size):
        """
            fmp_size: (List) [H, W]
        """
        # generate grid cells
        fmp_h, fmp_w = fmp_size
        # [KA, 2]
        anchor_size = self.anchor_size[level]

        anchor_y, anchor_x = torch.meshgrid([torch.arange(fmp_h), torch.arange(fmp_w)])
        # [H, W, 2] -> [HW, 2]
        anchor_xy = torch.stack([anchor_x, anchor_y], dim=-1).float().view(-1, 2) + 0.5
        # [HW, 2] -> [HW, 1, 2] -> [HW, KA, 2] 
        anchor_xy = anchor_xy[:, None, :].repeat(1, self.num_anchors, 1)
        anchor_xy *= self.stride[level]

        # [KA, 2] -> [1, KA, 2] -> [HW, KA, 2]
        anchor_wh = anchor_size[None, :, :].repeat(fmp_h*fmp_w, 1, 1)

        # [HW, KA, 4] -> [M, 4], M = HW x KA
        anchor_boxes = torch.cat([anchor_xy, anchor_wh], dim=-1)
        anchor_boxes = anchor_boxes.view(-1, 4)

        return anchor_boxes
        
    def decode_boxes(self, anchor_boxes, pred_reg):
        """
            anchor_boxes: (List[Tensor]) [1, M, 4] or [M, 4]
            pred_reg:     (List[Tensor]) [B, M, 4] or [M, 4]
        """
        # x = x_anchor + dx * w_anchor
        # y = y_anchor + dy * h_anchor
        pred_ctr_offset = pred_reg[..., :2] * anchor_boxes[..., 2:]
        pred_ctr_xy = anchor_boxes[..., :2] + pred_ctr_offset

        # w = w_anchor * exp(tw)
        # h = h_anchor * exp(th)
        pred_dwdh = pred_reg[..., 2:]
        pred_dwdh = torch.clamp(pred_dwdh, max=self.DEFAULT_SCALE_CLAMP)
        pred_wh = anchor_boxes[..., 2:] * pred_dwdh.exp()

        # convert [x, y, w, h] -> [x1, y1, x2, y2]
        pred_x1y1 = pred_ctr_xy - 0.5 * pred_wh
        pred_x2y2 = pred_ctr_xy + 0.5 * pred_wh
        pred_box = torch.cat([pred_x1y1, pred_x2y2], dim=-1)

        return pred_box

    def forward(self, pyramid_feats, mask=None):
        all_masks = []
        all_anchors = []
        all_cls_preds = []
        all_reg_preds = []
        all_box_preds = []
        for level, feat in enumerate(pyramid_feats):
            # ------------------- Decoupled head -------------------
            cls_feat = self.cls_heads(feat)
            reg_feat = self.reg_heads(feat)

            # ------------------- Generate anchor box -------------------
            B, _, H, W = cls_feat.size()
            fmp_size = [H, W]
            anchor_boxes = self.generate_anchors(level, fmp_size)   # [M, 4]
            anchor_boxes = anchor_boxes.to(cls_feat.device)

            # ------------------- Predict -------------------
            cls_pred = self.cls_pred(cls_feat)
            reg_pred = self.reg_pred(reg_feat)

            # ------------------- Process preds -------------------
            ## [B, C, H, W] -> [B, H, W, C] -> [B, M, C]
            cls_pred = cls_pred.permute(0, 2, 3, 1).contiguous().view(B, -1, self.num_classes)
            reg_pred = reg_pred.permute(0, 2, 3, 1).contiguous().view(B, -1, 4)
            ## Decode bbox
            box_pred = self.decode_boxes(anchor_boxes, reg_pred)
            ## Adjust mask
            if mask is not None:
                # [B, H, W]
                mask_i = torch.nn.functional.interpolate(mask[None], size=[H, W]).bool()[0]
                # [B, H, W] -> [B, M]
                mask_i = mask_i.flatten(1)
                # [B, HW] -> [B, HW, KA] -> [B, M], M= HW x KA
                mask_i = mask_i[..., None].repeat(1, 1, self.num_anchors).flatten(1)
                
                all_masks.append(mask_i)
                
            all_anchors.append(anchor_boxes)
            all_cls_preds.append(cls_pred)
            all_reg_preds.append(reg_pred)
            all_box_preds.append(box_pred)

        outputs = {"pred_cls": all_cls_preds,  # List [B, M, C]
                   "pred_reg": all_reg_preds,  # List [B, M, 4]
                   "pred_box": all_box_preds,  # List [B, M, 4]
                   "anchors": all_anchors,     # List [B, M, 2]
                   "strides": self.stride,
                   "mask": all_masks}          # List [B, M,]

        return outputs 