import torch
import torch.nn.functional as F
from torchvision.ops.boxes import box_area
from scipy.optimize import linear_sum_assignment


def box_iou(boxes1, boxes2):
    area1 = box_area(boxes1)
    area2 = box_area(boxes2)

    lt = torch.max(boxes1[:, None, :2], boxes2[:, :2])  # [N,M,2]
    rb = torch.min(boxes1[:, None, 2:], boxes2[:, 2:])  # [N,M,2]

    wh = (rb - lt).clamp(min=0)  # [N,M,2]
    inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]

    union = area1[:, None] + area2 - inter

    iou = inter / union
    return iou, union


class SimOTAMatcher(object):
    """
        This code referenced to https://github.com/open-mmlab/mmyolo/models/task_modules/assigners/batch_dsl_assigner.py
    """
    def __init__(self, num_classes, topk_candidate=10):
        self.num_classes = num_classes
        self.topk_candidate = topk_candidate

    @torch.no_grad()
    def __call__(self, anchors, pred_cls, pred_box, gt_labels, gt_bboxes):
        # number of groundtruth
        num_gt = len(gt_labels)

        # check gt
        if num_gt == 0 or gt_bboxes.max().item() == 0.:
            cls_targets = gt_labels.new_full(pred_cls[..., 0].shape, self.num_classes, dtype=torch.long)
            box_targets = gt_bboxes.new_full(pred_box.shape, 0)
            iou_targets = gt_bboxes.new_full(pred_cls[..., 0].shape, 0)

            return cls_targets, box_targets, iou_targets
        
        # get inside points: [N, M]
        is_in_gt = self.find_inside_points(gt_bboxes, anchors)
        valid_mask = is_in_gt.sum(dim=0) > 0  # [M,]

        # ----------------------------------- Regression cost -----------------------------------
        pair_wise_ious, _ = box_iou(gt_bboxes, pred_box)  # [N, M]
        pair_wise_reg_loss = -torch.log(pair_wise_ious + 1e-8)

        # ----------------------------------- Classification cost -----------------------------------
        ## select the predicted scores corresponded to the gt_labels
        pairwise_pred_scores = pred_cls.permute(1, 0)  # [M, C] -> [C, M]
        pairwise_pred_scores = pairwise_pred_scores[gt_labels.long(), :].float()   # [N, M]
        ## scale factor
        scale_factor = (pair_wise_ious - pairwise_pred_scores.sigmoid()).abs().pow(2.0)
        ## cls cost
        pair_wise_cls_loss = F.binary_cross_entropy_with_logits(
            pairwise_pred_scores, pair_wise_ious,
            reduction="none") * scale_factor # [N, M]
            
        del pairwise_pred_scores

        ## foreground cost matrix
        cost_matrix = pair_wise_cls_loss + 3.0 * pair_wise_reg_loss
        max_pad_value = torch.ones_like(cost_matrix) * 1e9
        cost_matrix = torch.where(valid_mask[None].repeat(num_gt, 1), cost_matrix, max_pad_value)

        # ----------------------------------- Dynamic label assignment -----------------------------------
        (
            matched_pred_ious,
            matched_gt_inds,
            fg_mask_inboxes
        ) = self.dynamic_k_matching(
            cost_matrix,
            pair_wise_ious,
            num_gt
            )
        del pair_wise_cls_loss, cost_matrix, pair_wise_ious, pair_wise_reg_loss

        # ----------------------------------- Post-process assigned labels -----------------------------------
        cls_targets = gt_labels.new_full(pred_cls[..., 0].shape, self.num_classes)  # [M,]
        cls_targets[fg_mask_inboxes] = gt_labels[matched_gt_inds].squeeze(-1)
        cls_targets = cls_targets.long()  # [M,]

        box_targets = gt_bboxes.new_full(pred_box.shape, 0)        # [M, 4]
        box_targets[fg_mask_inboxes] = gt_bboxes[matched_gt_inds]  # [M, 4]

        iou_targets = gt_bboxes.new_full(pred_cls[..., 0].shape, 0) # [M,]
        iou_targets[fg_mask_inboxes] = matched_pred_ious            # [M,]
        
        return cls_targets, box_targets, iou_targets

    def find_inside_points(self, gt_bboxes, anchors):
        """
            gt_bboxes: Tensor -> [N, 2]
            anchors:   Tensor -> [M, 2]
        """
        num_anchors = anchors.shape[0]
        num_gt = gt_bboxes.shape[0]

        anchors_expand = anchors.unsqueeze(0).repeat(num_gt, 1, 1)           # [N, M, 2]
        gt_bboxes_expand = gt_bboxes.unsqueeze(1).repeat(1, num_anchors, 1)  # [N, M, 4]

        # offset
        lt = anchors_expand - gt_bboxes_expand[..., :2]
        rb = gt_bboxes_expand[..., 2:] - anchors_expand
        bbox_deltas = torch.cat([lt, rb], dim=-1)

        is_in_gts = bbox_deltas.min(dim=-1).values > 0

        return is_in_gts
    
    def dynamic_k_matching(self, cost_matrix, pairwise_ious, num_gt):
        """Use IoU and matching cost to calculate the dynamic top-k positive
        targets.

        Args:
            cost_matrix (Tensor): Cost matrix.
            pairwise_ious (Tensor): Pairwise iou matrix.
            num_gt (int): Number of gt.
            valid_mask (Tensor): Mask for valid bboxes.
        Returns:
            tuple: matched ious and gt indexes.
        """
        matching_matrix = torch.zeros_like(cost_matrix, dtype=torch.uint8)
        # select candidate topk ious for dynamic-k calculation
        candidate_topk = min(self.topk_candidate, pairwise_ious.size(1))
        topk_ious, _ = torch.topk(pairwise_ious, candidate_topk, dim=1)
        # calculate dynamic k for each gt
        dynamic_ks = torch.clamp(topk_ious.sum(1).int(), min=1)

        # sorting the batch cost matirx is faster than topk
        _, sorted_indices = torch.sort(cost_matrix, dim=1)
        for gt_idx in range(num_gt):
            topk_ids = sorted_indices[gt_idx, :dynamic_ks[gt_idx]]
            matching_matrix[gt_idx, :][topk_ids] = 1

        del topk_ious, dynamic_ks, topk_ids

        prior_match_gt_mask = matching_matrix.sum(0) > 1
        if prior_match_gt_mask.sum() > 0:
            cost_min, cost_argmin = torch.min(
                cost_matrix[:, prior_match_gt_mask], dim=0)
            matching_matrix[:, prior_match_gt_mask] *= 0
            matching_matrix[cost_argmin, prior_match_gt_mask] = 1

        # get foreground mask inside box and center prior
        fg_mask_inboxes = matching_matrix.sum(0) > 0
        matched_pred_ious = (matching_matrix *
                             pairwise_ious).sum(0)[fg_mask_inboxes]
        matched_gt_inds = matching_matrix[:, fg_mask_inboxes].argmax(0)

        return matched_pred_ious, matched_gt_inds, fg_mask_inboxes


class HungarianMatcher(object):
    def __init__(self, num_classes):
        self.num_classes = num_classes

    @torch.no_grad()
    def __call__(self, anchors, pred_cls, pred_box, gt_labels, gt_bboxes):
        num_gts = len(gt_labels)         # N
        num_anchors = anchors.shape[0]   # M

        # get inside points: [N, M]
        is_in_gt = self.find_inside_points(gt_bboxes, anchors)
        valid_mask = is_in_gt.sum(dim=0) > 0  # [M,]

        # ----------------------------------- Regression cost -----------------------------------
        pair_wise_ious, _ = box_iou(gt_bboxes, pred_box)  # [N, M]
        pair_wise_reg_loss = -torch.log(pair_wise_ious + 1e-8)

        # ----------------------------------- Classification cost -----------------------------------
        ## select the predicted scores corresponded to the gt_labels
        pairwise_pred_scores = pred_cls.permute(1, 0)  # [M, C] -> [C, M]
        pairwise_pred_scores = pairwise_pred_scores[gt_labels.long(), :].float()   # [N, M]
        ## scale factor
        scale_factor = (pair_wise_ious - pairwise_pred_scores.sigmoid()).abs().pow(2.0)
        ## cls cost
        pair_wise_cls_loss = F.binary_cross_entropy_with_logits(
            pairwise_pred_scores, pair_wise_ious,
            reduction="none") * scale_factor # [N, M]
            
        del pairwise_pred_scores

        # Final cost: [N, M]
        cost_matrix = pair_wise_cls_loss + 3.0 * pair_wise_reg_loss
        max_pad_value = torch.ones_like(cost_matrix) * 1e9
        cost_matrix = torch.where(valid_mask[None].repeat(num_gts, 1), cost_matrix, max_pad_value)
        cost_matrix = cost_matrix.cpu()
        
        # solve the one-to-one assignment
        indices = linear_sum_assignment(cost_matrix)
        gt_indices, pred_indices = indices[0].tolist(), indices[1].tolist()

        fg_mask = pred_cls.new_zeros(num_anchors).bool()
        fg_mask[pred_indices] = True

        # [M, C]
        cls_target = gt_labels.new_full(pred_cls[..., 0].shape, self.num_classes, dtype=torch.long)
        cls_target[pred_indices] = gt_labels

        # [M, 4]
        box_target = gt_bboxes.new_full(pred_box.shape, 0)
        box_target[pred_indices] = gt_bboxes

        # [M,]
        iou_target = gt_bboxes.new_full(pred_box[..., 0].shape, 0)
        iou_target[pred_indices] = pair_wise_ious[gt_indices, pred_indices]        

        return cls_target, box_target, iou_target

    def find_inside_points(self, gt_bboxes, anchors):
        """
            gt_bboxes: Tensor -> [N, 2]
            anchors:   Tensor -> [M, 2]
        """
        num_anchors = anchors.shape[0]
        num_gt = gt_bboxes.shape[0]

        anchors_expand = anchors.unsqueeze(0).repeat(num_gt, 1, 1)           # [N, M, 2]
        gt_bboxes_expand = gt_bboxes.unsqueeze(1).repeat(1, num_anchors, 1)  # [N, M, 4]

        # offset
        lt = anchors_expand - gt_bboxes_expand[..., :2]
        rb = gt_bboxes_expand[..., 2:] - anchors_expand
        bbox_deltas = torch.cat([lt, rb], dim=-1)

        is_in_gts = bbox_deltas.min(dim=-1).values > 0

        return is_in_gts
    

def build_matcher(cfg, num_classes):
    matcher_cfg = cfg['matcher_hpy']
    if cfg['matcher'] == 'simota':
        matcher = SimOTAMatcher(num_classes, matcher_cfg['topk_candidate'])
    elif cfg['matcher'] == 'hungarian':
        matcher = HungarianMatcher(num_classes)

    return matcher


if __name__ == "__main__":
    import torch
    
    num_gts = 6
    num_anchors = 16
    num_classes = 7
    # [H, W, 2] -> [HW, 2]
    anchor_y, anchor_x = torch.meshgrid([torch.arange(4), torch.arange(4)])
    anchors = torch.stack([anchor_x, anchor_y], dim=-1).float().view(-1, 2) + 0.5

    pred_cls = torch.randn([num_anchors, num_classes])
    pred_box = torch.randn([num_anchors, 4])

    gt_labels = torch.randint(0, num_classes, [num_gts])
    gt_bboxes = torch.as_tensor([[1. * 1.5 * i, 2. * 1.5 * i, 3. * 1.5 * i, 4. * 1.5 * i]  for i in range(num_gts)])

    # print(gt_labels)
    # print(pred_cls[:num_gts].sigmoid())
    matcher = HungarianMatcher(num_classes)
    matcher(anchors, pred_cls, pred_box, gt_labels, gt_bboxes)

