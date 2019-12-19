import torch
import torch.nn as nn
import random
import numpy as np
from module.encoder import EncoderCNN, EncoderLabels
from module.transformer_decoder import DecoderTransformer
# from module.multihead_attention import MultiheadAttention
from utils.metrics import softIoU, MaskedCrossEntropyCriterion
import pickle
import os
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

def get_model(args, ingr_vocab_size):

    # # build ingredients embedding
    # encoder_ingrs = EncoderLabels(args.embed_size, ingr_vocab_size,
    #                               args.dropout_encoder, scale_grad=False).to(device)
    # build image model
    encoder_image = EncoderCNN(args.embed_size, args.dropout_encoder, args.image_model)

    decoder = DecoderTransformer(args.embed_size, ingr_vocab_size,
                                 dropout=args.dropout_decoder_r, seq_length=args.maxseqlen,
                                 num_instrs=args.maxnuminstrs,
                                 attention_nheads=args.n_att, num_layers=args.transf_layers,
                                 normalize_before=True,
                                 normalize_inputs=False,
                                 last_ln=False,
                                 scale_embed_grad=False)

    # ingr_decoder = DecoderTransformer(args.embed_size, ingr_vocab_size, dropout=args.dropout_decoder_i,
    #                                   seq_length=args.maxnumlabels,
    #                                   num_instrs=1, attention_nheads=args.n_att_ingrs,
    #                                   pos_embeddings=False,
    #                                   num_layers=args.transf_layers_ingrs,
    #                                   learned=False,
    #                                   normalize_before=True,
    #                                   normalize_inputs=True,
    #                                   last_ln=True,
    #                                   scale_embed_grad=False)
    
    # recipe loss
    criterion = MaskedCrossEntropyCriterion(ignore_index=[instrs_vocab_size-1], reduce=False)

    # ingredients loss
    label_loss = nn.BCELoss(reduce=False)
    eos_loss = nn.BCELoss(reduce=False)

    model = InverseCookingModel(decoder, encoder_image,
                                crit=criterion, crit_ingr=label_loss, crit_eos=eos_loss,
                                pad_value=ingr_vocab_size-1,
                                ingrs_only=args.ingrs_only, recipe_only=args.recipe_only,
                                label_smoothing=args.label_smoothing_ingr)

    return model

class InverseCookingModel(nn.Module):
    def __init__(self, recipe_decoder, image_encoder,
                 crit=None, crit_ingr=None, crit_eos=None,
                 pad_value=0, ingrs_only=True,
                 recipe_only=False, label_smoothing=0.0):

        super(InverseCookingModel, self).__init__()

        self.recipe_decoder = recipe_decoder
        self.image_encoder = image_encoder
        self.crit = crit
        self.crit_ingr = crit_ingr
        self.pad_value = pad_value
        self.ingrs_only = ingrs_only
        self.recipe_only = recipe_only
        self.crit_eos = crit_eos
        self.label_smoothing = label_smoothing


    def forward(self, img_inputs, captions, target_ingrs,
        sample=False, keep_cnn_gradients=False):

        if sample:
            return self.sample(img_inputs, greedy=True)

        targets = captions[:, 1:]
        targets = targets.contiguous().view(-1)

        img_features = self.image_encoder(img_inputs, keep_cnn_gradients)

        losses = {}
        target_one_hot = label2onehot(target_ingrs, self.pad_value)
        target_one_hot_smooth = label2onehot(target_ingrs, self.pad_value)

        # ingredient prediction
        if not self.recipe_only:
            target_one_hot_smooth[target_one_hot_smooth == 1] = (1-self.label_smoothing)
            target_one_hot_smooth[target_one_hot_smooth == 0] = self.label_smoothing / target_one_hot_smooth.size(-1)

            # decode ingredients with transformer
            # autoregressive mode for ingredient decoder
            ingr_ids, ingr_logits = self.ingredient_decoder.sample(None, None, greedy=True,
                                                                   temperature=1.0, img_features=img_features,
                                                                   first_token_value=0, replacement=False)

            ingr_logits = torch.nn.functional.softmax(ingr_logits, dim=-1)

            # find idxs for eos ingredient
            # eos probability is the one assigned to the first position of the softmax
            eos = ingr_logits[:, :, 0]
            target_eos = ((target_ingrs == 0) ^ (target_ingrs == self.pad_value))

            eos_pos = (target_ingrs == 0)
            eos_head = ((target_ingrs != self.pad_value) & (target_ingrs != 0))

            # select transformer steps to pool from
            mask_perminv = mask_from_eos(target_ingrs, eos_value=0, mult_before=False)
            ingr_probs = ingr_logits * mask_perminv.float().unsqueeze(-1)

            ingr_probs, _ = torch.max(ingr_probs, dim=1)

            # ignore predicted ingredients after eos in ground truth
            ingr_ids[mask_perminv == 0] = self.pad_value

            ingr_loss = self.crit_ingr(ingr_probs, target_one_hot_smooth)
            ingr_loss = torch.mean(ingr_loss, dim=-1)

            losses['ingr_loss'] = ingr_loss

            # cardinality penalty
            losses['card_penalty'] = torch.abs((ingr_probs*target_one_hot).sum(1) - target_one_hot.sum(1)) + \
                                     torch.abs((ingr_probs*(1-target_one_hot)).sum(1))

            eos_loss = self.crit_eos(eos, target_eos.float())

            mult = 1/2
            # eos loss is only computed for timesteps <= t_eos and equally penalizes 0s and 1s
            losses['eos_loss'] = mult*(eos_loss * eos_pos.float()).sum(1) / (eos_pos.float().sum(1) + 1e-6) + \
                                 mult*(eos_loss * eos_head.float()).sum(1) / (eos_head.float().sum(1) + 1e-6)
            # iou
            pred_one_hot = label2onehot(ingr_ids, self.pad_value)
            # iou sample during training is computed using the true eos position
            losses['iou'] = softIoU(pred_one_hot, target_one_hot)

        if self.ingrs_only:
            return losses

        # encode ingredients
        target_ingr_feats = self.ingredient_encoder(target_ingrs)
        target_ingr_mask = mask_from_eos(target_ingrs, eos_value=0, mult_before=False)

        target_ingr_mask = target_ingr_mask.float().unsqueeze(1)

        outputs, ids = self.recipe_decoder(target_ingr_feats, target_ingr_mask, captions, img_features)

        outputs = outputs[:, :-1, :].contiguous()
        outputs = outputs.view(outputs.size(0) * outputs.size(1), -1)

        loss = self.crit(outputs, targets)

        losses['recipe_loss'] = loss

        return losses

    def sample(self, img_inputs, greedy=True, temperature=1.0, beam=-1, true_ingrs=None):

        outputs = dict()

        img_features = self.image_encoder(img_inputs)

        if not self.recipe_only:
            ingr_ids, ingr_probs = self.ingredient_decoder.sample(None, None, greedy=True, temperature=temperature,
                                                                  beam=-1,
                                                                  img_features=img_features, first_token_value=0,
                                                                  replacement=False)

            # mask ingredients after finding eos
            sample_mask = mask_from_eos(ingr_ids, eos_value=0, mult_before=False)
            ingr_ids[sample_mask == 0] = self.pad_value

            outputs['ingr_ids'] = ingr_ids
            outputs['ingr_probs'] = ingr_probs.data

            mask = sample_mask
            input_mask = mask.float().unsqueeze(1)
            input_feats = self.ingredient_encoder(ingr_ids)

        if self.ingrs_only:
            return outputs

        # option during sampling to use the real ingredients and not the predicted ones to infer the recipe
        if true_ingrs is not None:
            input_mask = mask_from_eos(true_ingrs, eos_value=0, mult_before=False)
            true_ingrs[input_mask == 0] = self.pad_value
            input_feats = self.ingredient_encoder(true_ingrs)
            input_mask = input_mask.unsqueeze(1)

        ids, probs = self.recipe_decoder.sample(input_feats, input_mask, greedy, temperature, beam, img_features, 0,
                                                last_token_value=1)

        outputs['recipe_probs'] = probs.data
        outputs['recipe_ids'] = ids

        return outputs