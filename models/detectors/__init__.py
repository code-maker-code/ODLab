# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import torch

from .fcos.build  import build_fcos, build_fcos_rt
from .yolof.build import build_yolof


def build_model(args, cfg, is_val=False):
    # ------------ build object detector ------------
    ## RT-FCOS    
    if   'fcos_rt' in args.model:
        model, criterion = build_fcos_rt(cfg, is_val)
    ## FCOS    
    elif 'fcos' in args.model:
        model, criterion = build_fcos(cfg, is_val)
    ## YOLOF    
    elif 'yolof' in args.model:
        model, criterion = build_yolof(cfg, is_val)
    else:
        raise NotImplementedError("Unknown detector: {}".args.model)
    
    if is_val:
        # ------------ Keep training from the given weight ------------
        if args.resume is not None:
            print('Load model from the checkpoint: ', args.resume)
            checkpoint = torch.load(args.resume, map_location='cpu')
            # checkpoint state dict
            checkpoint_state_dict = checkpoint.pop("model")
            model.load_state_dict(checkpoint_state_dict)

        return model, criterion

    else:      
        return model
    