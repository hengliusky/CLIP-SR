import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
from collections import OrderedDict
from lib.utils import dummy_context_mgr
import math
from .swin import SwinIR,RSTB
from torch.nn.utils import spectral_norm

class CLIP_IMG_ENCODER(nn.Module):
    def __init__(self, CLIP):
        super(CLIP_IMG_ENCODER, self).__init__()
        model = CLIP.visual
      
        self.define_module(model)
        for param in self.parameters():
            param.requires_grad = False

    def define_module(self, model):
        self.conv1 = model.conv1
        self.class_embedding = model.class_embedding
        self.positional_embedding = model.positional_embedding
        self.ln_pre = model.ln_pre
        self.transformer = model.transformer
        self.ln_post = model.ln_post
        self.proj = model.proj

    @property
    def dtype(self):
        return self.conv1.weight.dtype

    def transf_to_CLIP_input(self,inputs):
        device = inputs.device
        if len(inputs.size()) != 4:
            raise ValueError('Expect the (B, C, X, Y) tensor.')
        else:
            mean = torch.tensor([0.48145466, 0.4578275, 0.40821073])\
                .unsqueeze(-1).unsqueeze(-1).unsqueeze(0).to(device)
            var = torch.tensor([0.26862954, 0.26130258, 0.27577711])\
                .unsqueeze(-1).unsqueeze(-1).unsqueeze(0).to(device)
            inputs = F.interpolate(inputs*0.5+0.5, size=(224, 224))
            inputs = ((inputs+1)*0.5-mean)/var
            return inputs

    def forward(self, img: torch.Tensor):
        x = self.transf_to_CLIP_input(img)
        x = x.type(self.dtype)
        x = self.conv1(x)
        grid =  x.size(-1)
        x = x.reshape(x.shape[0], x.shape[1], -1)
        x = x.permute(0, 2, 1)
        x = torch.cat([self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)  # shape = [*, grid ** 2 + 1, width]
        x = x + self.positional_embedding.to(x.dtype)
        x = self.ln_pre(x)
        x = x.permute(1, 0, 2)
        selected = [1,4,8]
        local_features = []
        for i in range(12):
            x = self.transformer.resblocks[i](x)
            if i in selected:
                local_features.append(x.permute(1, 0, 2)[:, 1:, :].permute(0, 2, 1).reshape(-1, 768, grid, grid).contiguous().type(img.dtype))
        x = x.permute(1, 0, 2)
        x = self.ln_post(x[:, 0, :])
        if self.proj is not None:
            x = x @ self.proj
        return torch.stack(local_features, dim=1), x.type(img.dtype)


class CLIP_TXT_ENCODER(nn.Module):
    def __init__(self, CLIP):
        super(CLIP_TXT_ENCODER, self).__init__()
        self.define_module(CLIP)
        for param in self.parameters():
            param.requires_grad = False

    def define_module(self, CLIP):
        self.transformer = CLIP.transformer
        self.vocab_size = CLIP.vocab_size
        self.token_embedding = CLIP.token_embedding
        self.positional_embedding = CLIP.positional_embedding
        self.ln_final = CLIP.ln_final
        self.text_projection = CLIP.text_projection

    @property
    def dtype(self):
        return self.transformer.resblocks[0].mlp.c_fc.weight.dtype

    def forward(self, text):
        x = self.token_embedding(text).type(self.dtype)
        x = x + self.positional_embedding.type(self.dtype)
        x = x.permute(1, 0, 2)
        x = self.transformer(x)
        x = x.permute(1, 0, 2)
        x = self.ln_final(x).type(self.dtype)
        sent_emb = x[torch.arange(x.shape[0]), text.argmax(dim=-1)] @ self.text_projection
        return sent_emb, x


class CLIP_Mapper(nn.Module):
    def __init__(self, CLIP):
        super(CLIP_Mapper, self).__init__()
        model = CLIP.visual
        # print(model)
        self.define_module(model)
        for param in model.parameters():
            param.requires_grad = False

    def define_module(self, model):
        self.conv1 = model.conv1
        self.class_embedding = model.class_embedding
        self.positional_embedding = model.positional_embedding
        self.ln_pre = model.ln_pre
        self.transformer = model.transformer

    @property
    def dtype(self):
        return self.conv1.weight.dtype

    def forward(self, img: torch.Tensor, prompts: torch.Tensor):

        x = img.type(self.dtype)

        prompts = prompts.type(self.dtype)
        grid = x.size(-1)

        x = x.reshape(x.shape[0], x.shape[1], -1)
        x = x.permute(0, 2, 1)
        x = torch.cat([self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)
        x = x + self.positional_embedding.to(x.dtype)
        x = self.ln_pre(x)
        x = x.permute(1, 0, 2)
        selected = [1,2,3,4,5,6,7,8]
        begin, end = 0, 12
        prompt_idx = 0
        for i in range(begin, end):
            if i in selected:
                prompt = prompts[:,prompt_idx,:].unsqueeze(0)
                prompt_idx = prompt_idx+1
                x = torch.cat((x,prompt), dim=0)
                x = self.transformer.resblocks[i](x)
                x = x[:-1,:,:]
            else:
                x = self.transformer.resblocks[i](x)
        return x.permute(1, 0, 2)[:, 1:, :].permute(0, 2, 1).reshape(-1, 768, grid, grid).contiguous().type(img.dtype)


class CLIP_Adapter(nn.Module):
    def __init__(self, in_ch, mid_ch, out_ch, G_ch, CLIP_ch, cond_dim, k, s, p, map_num, CLIP):
        super(CLIP_Adapter, self).__init__()
        self.CLIP_ch = CLIP_ch
        self.FBlocks = nn.ModuleList([])
        self.FBlocks.append(M_Block(in_ch, mid_ch, out_ch, cond_dim, k, s, p))
        for i in range(map_num-1):
            self.FBlocks.append(M_Block(out_ch, mid_ch, out_ch, cond_dim, k, s, p))
        self.conv_fuse = nn.Conv2d(512, CLIP_ch, 2, 1, 0)
        self.CLIP_ViT = CLIP_Mapper(CLIP)
        self.conv = nn.Conv2d(768, G_ch, 5, 1, 2)
        self.fc_prompt = nn.Linear(cond_dim, CLIP_ch*8)

    def forward(self,out,c,LR):
        prompts = self.fc_prompt(c).view(c.size(0),-1,self.CLIP_ch)
        fuse_feat = self.conv_fuse(LR)
        map_feat = self.CLIP_ViT(fuse_feat,prompts)
        return self.conv(fuse_feat+0.1*map_feat)



class NetG(nn.Module):
    def __init__(self, ngf, nz, cond_dim, imsize, ch_size, mixed_precision, CLIP):
        super(NetG, self).__init__()
        self.ngf = ngf
        self.mixed_precision = mixed_precision
        self.code_sz, self.code_ch, self.mid_ch = 7, 64, 32
        self.CLIP_ch = 768
        self.fc_code = nn.Linear(nz, self.code_sz*self.code_sz*self.code_ch)
        self.mapping = CLIP_Adapter(self.code_ch, self.mid_ch, self.code_ch, ngf*8, self.CLIP_ch, cond_dim, 3, 1, 1, 4, CLIP)
        self.GBlocks = nn.ModuleList([])
        self.TBlocks=  M_Block(512, 32, 512, cond_dim, 3, 1, 1)
        in_out_pairs = list(get_G_in_out_chs(ngf, 256))
        self.embed_dim=256
     
       
        imsize = 4
        for idx, (in_ch, out_ch) in enumerate(in_out_pairs):
            if idx<(len(in_out_pairs)-1):
                imsize = imsize*2
            else:
                imsize = 256
            self.GBlocks.append(G_Block(cond_dim, in_ch, out_ch, imsize))
        self.to_rgb = nn.Sequential(
            nn.LeakyReLU(0.2,inplace=True),
            nn.Conv2d(3, ch_size, 3, 1, 1),
            )

        self.head = nn.Sequential(
           nn.Conv2d(3, 256, 3, 1, 1))


        self.tail = nn.Sequential(
            nn.Conv2d(64, 3, 3, 1, 1)
        )
        self.c1= nn.Conv2d(in_channels=3,
                    out_channels=256, kernel_size=3, stride=1, padding=1)
        self.R=nn.PReLU()
        self.c2= nn.Conv2d(in_channels=256,
                    out_channels=512, kernel_size=3, stride=2, padding=1)
        self.c3= nn.Conv2d(in_channels=512,
                    out_channels=512, kernel_size=3, stride=2, padding=1)
        self.c4= nn.Conv2d(in_channels=512,
                    out_channels=512, kernel_size=3, 
                    stride=2, padding=1)

        self.conv_before_upsample = nn.Sequential(nn.Conv2d(256, 64, 3, 1, 1),
                                                      nn.LeakyReLU(inplace=True))
        self.upsample = Upsample(4, 64)

        # self.up = nn.Upsample();
        embed_dim=self.embed_dim
        self.conv_after_body = nn.Sequential(nn.Conv2d(embed_dim, embed_dim // 4, 3, 1, 1),
                                                 nn.LeakyReLU(negative_slope=0.2, inplace=True),
                                                 nn.Conv2d(embed_dim // 4, embed_dim // 4, 1, 1, 0),
                                                 nn.LeakyReLU(negative_slope=0.2, inplace=True),
                                                 nn.Conv2d(embed_dim // 4, embed_dim, 3, 1, 1))
    def forward_swin(self,x):
        
        x_size = (x.shape[2], x.shape[3])
        chans=x.shape[1]
        a=SwinIR(in_chans=x.shape[1],embed_dim=x.shape[1],img_size=(x.shape[2],x.shape[3]), patch_norm=None)
        a.to(torch.device("cuda:0"))
        x=a.patch_embed(x)
        if a.ape:
            x = x + a.absolute_pos_embed
        x = a.pos_drop(x)
        patches_resolution=a.patch_embed.patches_resolution
               
        layer=RSTB(dim=chans,
                         input_resolution=(patches_resolution[0],
                                           patches_resolution[1]),
                         depth=1,
                         num_heads=4,
                         window_size=4,
                         mlp_ratio=4.,
                         qkv_bias=True, qk_scale=None,
                         drop=0., attn_drop=0.,
                          # no impact on SR results
                         norm_layer=nn.LayerNorm,
                         downsample=None,
                         use_checkpoint=False,
                         img_size=x_size,
                         patch_size=1,
                         resi_connection='1conv')
        layer.to(torch.device("cuda:0"))
        x=layer(x,x_size)
        x = a.norm(x)
        x = a.patch_unembed(x, x_size)
        
        return x
        
    def forward(self, LR, c, eval=False):
        with torch.cuda.amp.autocast() if self.mixed_precision and not eval else dummy_context_mgr() as mp:
            LR=F.interpolate(LR, size=(64, 64))
            H, W = LR.shape[2:]
     
            LR_residual=self.head(LR)
            
            R1=self.c1(LR)
            R2=self.c2(self.R(R1))
            R3=self.c3(self.R(R2))
            R4=self.c4(self.R(R3))
            c=c.float()          
            LR_fuse=self.TBlocks(R4,c)

            out = self.mapping(self.fc_code(c).view(c.size(0), self.code_ch, self.code_sz, self.code_sz), c,LR_fuse)

            i=1
            for GBlock in self.GBlocks:           
                if i==1:               
                    out = GBlock(R4, c)       
                    out=R4+out
                if i==2:
                    out = GBlock(out, c)
                    out=R3+out
                if i==3:
                    out = GBlock(out, c)
                    out=R2+out  
                if i==4:
                    out = GBlock(out, c)
                    out=R1+out                   
                i=i+1

            out=self.conv_after_body(out)+LR_residual
           
            out=self.conv_before_upsample(out)
            out=self.upsample(out)
            out = self.tail(out)

        return out[:,:,:H*4, :W*4]


class Upsample(nn.Sequential):
    def __init__(self, scale, num_feat):
        m = []
        if (scale & (scale - 1)) == 0:  # scale = 2^n
            for _ in range(int(math.log(scale, 2))):
                m.append(nn.Conv2d(num_feat, 4 * num_feat, 3, 1, 1))
                m.append(nn.PixelShuffle(2))
        elif scale == 3:
            m.append(nn.Conv2d(num_feat, 9 * num_feat, 3, 1, 1))
            m.append(nn.PixelShuffle(3))
        else:
            raise ValueError(f'scale {scale} is not supported. ' 'Supported scales: 2^n and 3.')
        super(Upsample, self).__init__(*m)

class NetD(nn.Module):
    def __init__(self, ndf, imsize, ch_size, mixed_precision):
        super(NetD, self).__init__()
        self.mixed_precision = mixed_precision
        self.DBlocks = nn.ModuleList([
            D_Block(768, 768, 3, 1, 1, res=True, CLIP_feat=True),
            D_Block(768, 768, 3, 1, 1, res=True, CLIP_feat=True),
        ])
        self.main = D_Block(768, 512, 3, 1, 1, res=True, CLIP_feat=False)

    def forward(self, h):
        with torch.cuda.amp.autocast() if self.mixed_precision else dummy_context_mgr() as mpc:
            out = h[:,0]
            for idx in range(len(self.DBlocks)):
                out = self.DBlocks[idx](out, h[:,idx+1])
            out = self.main(out)
        return out


class NetC(nn.Module):
    def __init__(self, ndf, cond_dim, mixed_precision):
        super(NetC, self).__init__()
        self.cond_dim = cond_dim
        self.mixed_precision = mixed_precision
        self.joint_conv = nn.Sequential(
            nn.Conv2d(512+512, 128, 4, 1, 0, bias=False),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(128, 1, 4, 1, 0, bias=False),
            )

    def forward(self, out, cond):
        with torch.cuda.amp.autocast() if self.mixed_precision else dummy_context_mgr() as mpc:
            cond = cond.view(-1, self.cond_dim, 1, 1)
            cond = cond.repeat(1, 1, 7, 7)
            h_c_code = torch.cat((out, cond), 1)
            out = self.joint_conv(h_c_code)
        return out

class M_Block(nn.Module):
    def __init__(self, in_ch, mid_ch, out_ch, cond_dim, k, s, p):
        super(M_Block, self).__init__()
        self.conv1 = nn.Conv2d(in_ch, mid_ch, k, s, p)
        self.fuse1 = DFBLK(cond_dim, mid_ch)
        self.conv2 = nn.Conv2d(mid_ch, out_ch, k, s, p)
        self.fuse2 = DFBLK(cond_dim, out_ch)
        self.learnable_sc = in_ch != out_ch
        if self.learnable_sc:
            self.c_sc = nn.Conv2d(in_ch, out_ch, 1, stride=1, padding=0)

    def shortcut(self, x):
        if self.learnable_sc:
            x = self.c_sc(x)
        return x

    def residual(self, h, text):
        h = self.conv1(h)
        h = self.fuse1(h, text)
        h = self.conv2(h)
        h = self.fuse2(h, text)
        return h

    def forward(self, h, c):
        return self.shortcut(h) + self.residual(h, c)



class G_Block(nn.Module):
    def __init__(self, cond_dim, in_ch, out_ch, imsize):
        super(G_Block, self).__init__()
        self.imsize = imsize
        self.learnable_sc = in_ch != out_ch 
        self.c1 = nn.Conv2d(in_ch, out_ch, 3, 1, 1)
        self.c2 = nn.Conv2d(out_ch, out_ch, 3, 1, 1)
        self.fuse1 = DFBLK(cond_dim, in_ch)
        self.fuse2 = DFBLK(cond_dim, out_ch)
        if self.learnable_sc:
            self.c_sc = nn.Conv2d(in_ch,out_ch, 1, stride=1, padding=0)

    def shortcut(self, x):
        if self.learnable_sc:
            x = self.c_sc(x)
        return x

    def residual(self, h, y):
        h = self.fuse1(h, y)
        h = self.c1(h)
        h = self.fuse2(h, y)
        h = self.c2(h)
        return h

    def forward(self, h, y):
        h = F.interpolate(h, size=(self.imsize, self.imsize))
        return self.shortcut(h) + self.residual(h, y)


class D_Block(nn.Module):
    def __init__(self, fin, fout, k, s, p, res, CLIP_feat):
        super(D_Block, self).__init__()
        self.res, self.CLIP_feat = res, CLIP_feat
        self.learned_shortcut = (fin != fout)
        self.conv_r = nn.Sequential(
            nn.Conv2d(fin, fout, k, s, p, bias=False),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(fout, fout, k, s, p, bias=False),
            nn.LeakyReLU(0.2, inplace=True),
            )
        self.conv_s = nn.Conv2d(fin, fout, 1, stride=1, padding=0)
        if self.res==True:
            self.gamma = nn.Parameter(torch.zeros(1))
        if self.CLIP_feat==True:
            self.beta = nn.Parameter(torch.zeros(1))

    def forward(self, x, CLIP_feat=None):
        res = self.conv_r(x)
        if self.learned_shortcut:
            x = self.conv_s(x)
        if (self.res==True)and(self.CLIP_feat==True):
            return x + self.gamma*res + self.beta*CLIP_feat
        elif (self.res==True)and(self.CLIP_feat!=True):
            return x + self.gamma*res
        elif (self.res!=True)and(self.CLIP_feat==True):
            return x + self.beta*CLIP_feat
        else:
            return x


class DFBLK(nn.Module):
    def __init__(self, cond_dim, in_ch):
        super(DFBLK, self).__init__()
        self.affine0 = Affine(cond_dim, in_ch)
        self.affine1 = Affine(cond_dim, in_ch)

    def forward(self, x, y=None):
        h = self.affine0(x, y)
        h = nn.LeakyReLU(0.2,inplace=True)(h)
        h = self.affine1(h, y)
        h = nn.LeakyReLU(0.2,inplace=True)(h)
        return h


class QuickGELU(nn.Module):
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)


class Affine(nn.Module):
    def __init__(self, cond_dim, num_features):
        super(Affine, self).__init__()

        self.fc_gamma = nn.Sequential(OrderedDict([
            ('linear1',nn.Linear(cond_dim, num_features)),
            ('relu1',nn.ReLU(inplace=True)),
            ('linear2',nn.Linear(num_features, num_features)),
            ]))
        self.fc_beta = nn.Sequential(OrderedDict([
            ('linear1',nn.Linear(cond_dim, num_features)),
            ('relu1',nn.ReLU(inplace=True)),
            ('linear2',nn.Linear(num_features, num_features)),
            ]))
      
        self._initialize()

    def _initialize(self):
        nn.init.zeros_(self.fc_gamma.linear2.weight.data)
        nn.init.ones_(self.fc_gamma.linear2.bias.data)
        nn.init.zeros_(self.fc_beta.linear2.weight.data)
        nn.init.zeros_(self.fc_beta.linear2.bias.data)

    def forward(self, x, y=None):
        weight1 = self.fc_gamma(y)
        bias1 = self.fc_beta(y)

        if weight1.dim() == 1:
            weight1 = weight1.unsqueeze(0)
        if bias1.dim() == 1:
            bias1 = bias1.unsqueeze(0)
        size = x.size()

        weight1 = weight1.unsqueeze(-1).unsqueeze(-1).expand(size)
        bias1 = bias1.unsqueeze(-1).unsqueeze(-1).expand(size)

        first_stage=weight1 * x + bias1
        return first_stage


def get_G_in_out_chs(nf, imsize):
    layer_num = int(np.log2(imsize))-1
    channel_nums = [nf*min(2**idx, 8) for idx in range(layer_num)]
    channel_nums = channel_nums[::-1]
    in_out_pairs = zip(channel_nums[:-1], channel_nums[1:])
    return in_out_pairs


def get_D_in_out_chs(nf, imsize):
    layer_num = int(np.log2(imsize))-1
    channel_nums = [nf*min(2**idx, 8) for idx in range(layer_num)]
    in_out_pairs = zip(channel_nums[:-1], channel_nums[1:])
    return in_out_pairs
    





