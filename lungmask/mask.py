import numpy as np
import torch
from lungmask import utils
import SimpleITK as sitk
from .resunet import UNet
import warnings
import sys
import skimage
import logging

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
warnings.filterwarnings("ignore", category=UserWarning)

# stores urls and number of classes of the models
model_urls = {('unet', 'R231'): ('https://github.com/JoHof/lungmask/releases/download/v0.0/unet_r231-d5d2fc3d.pth', 3),
              ('unet', 'LTRCLobes'): (
                  'https://github.com/JoHof/lungmask/releases/download/v0.0/unet_ltrclobes-3a07043d.pth', 6),
              ('unet', 'R231CovidWeb'): (
                  'https://github.com/JoHof/lungmask/releases/download/v0.0/unet_r231covid-0de78a7e.pth', 3)}


def apply(image, model=None, force_cpu=False, batch_size=20, volume_postprocessing=True, noHU=False):
    resolver = apply_async(image, model, force_cpu, batch_size, volume_postprocessing, noHU)
    return resolver.resolve()


def apply_async(image, model=None, force_cpu=False, batch_size=20, volume_postprocessing=True, noHU=False):
    if model is None:
        model = get_model('unet', 'R231')
    
    numpy_mode = isinstance(image, np.ndarray)
    directions = None
    if numpy_mode:
        inimg_raw = image.copy()
    else:
        inimg_raw = sitk.GetArrayFromImage(image)
        directions = np.asarray(image.GetDirection())
        if len(directions) == 9:
            inimg_raw = np.flip(inimg_raw, np.where(directions[[0,4,8]][::-1]<0)[0])
    del image

    if force_cpu:
        device = torch.device('cpu')
    else:
        if torch.cuda.is_available():
            device = torch.device('cuda')
        else:
            logging.info("No GPU support available, will use CPU. Note, that this is significantly slower!")
            batch_size = 1
            device = torch.device('cpu')
    model.to(device)

    tvolslices = None
    xnew_box = None
    if not noHU:
        tvolslices, xnew_box = utils.preprocess(inimg_raw, resolution=[256, 256])
        tvolslices[tvolslices > 600] = 600
        tvolslices = np.divide((tvolslices + 1024), 1624)
    else:
        # support for non HU images. This is just a hack. The models were not trained with this in mind
        tvolslices = skimage.color.rgb2gray(inimg_raw)
        tvolslices = skimage.transform.resize(tvolslices, [256, 256])
        tvolslices = np.asarray([tvolslices*x for x in np.linspace(0.3,2,20)])
        tvolslices[tvolslices>1] = 1
        sanity = [(tvolslices[x]>0.6).sum()>25000 for x in range(len(tvolslices))]
        tvolslices = tvolslices[sanity]
    torch_ds_val = utils.LungLabelsDS_inf(tvolslices)
    dataloader_val = torch.utils.data.DataLoader(torch_ds_val, batch_size=batch_size, shuffle=False, num_workers=0,
                                                 pin_memory=False)

    res = []
    with torch.no_grad():
        for X in dataloader_val:
            X = X.float().to(device)
            prediction = model(X)
            res.append(torch.max(prediction, 1)[1])

    return MaskAsyncResolver(res, volume_postprocessing, numpy_mode, inimg_raw, tvolslices[0].shape, xnew_box, directions)


class MaskAsyncResolver(object):

    def __init__(self, results , volume_postprocessing: bool, numpy_mode: bool, inimg_raw, shape, xnew_box, directions):
        self.results = results
        self.volume_postprocessing = volume_postprocessing
        self.numpy_mode = numpy_mode
        self.shape = shape
        self.xnew_box = xnew_box
        self.inimg_raw = inimg_raw
        self.directions = directions

    def resolve(self):
        timage_res = np.empty((np.append(0, self.shape)), dtype=np.uint8)

        with torch.no_grad():
            for res in self.results:
                pls = res.detach().cpu().numpy().astype(np.uint8)
                timage_res = np.vstack((timage_res, pls))

        if self.volume_postprocessing:
            outmask = utils.postrocessing(timage_res)
        else:
            outmask = timage_res

        outmask = np.asarray(
            [utils.reshape_mask(outmask[i], self.xnew_box[i], self.inimg_raw.shape[1:])
            for i in range(outmask.shape[0])],
            dtype=np.uint8)
        if not self.numpy_mode:
            if len(self.directions) == 9:
                outmask = np.flip(outmask, np.where(self.directions[[0, 4, 8]][::-1] < 0)[0])
        return outmask.astype(np.uint8)


def get_model(modeltype, modelname, modelpath=None, n_classes=3):
    if modelpath is None:
        model_url, n_classes = model_urls[(modeltype, modelname)]
        state_dict = torch.hub.load_state_dict_from_url(model_url, progress=True, map_location=torch.device('cpu'))
    else:
        state_dict = torch.load(modelpath, map_location=torch.device('cpu'))

    if modeltype == 'unet':
        model = UNet(n_classes=n_classes, padding=True, depth=5, up_mode='upsample', batch_norm=True, residual=False)
    elif modeltype == 'resunet':
        model = UNet(n_classes=n_classes, padding=True, depth=5, up_mode='upsample', batch_norm=True, residual=True)
    else:
        logging.exception(f"Model {modelname} not known")
    model.load_state_dict(state_dict)
    model.eval()
    return model


def apply_fused(image, basemodel = 'LTRCLobes', fillmodel = 'R231', force_cpu=False, batch_size=20, volume_postprocessing=True, noHU=False):
    '''Will apply basemodel and use fillmodel to mitiage false negatives'''
    mdl_r = get_model('unet',fillmodel)
    mdl_l = get_model('unet',basemodel)
    logging.info("Apply: %s" % basemodel)
    res_l = apply(image, mdl_l, force_cpu=force_cpu, batch_size=batch_size,  volume_postprocessing=volume_postprocessing, noHU=noHU)
    logging.info("Apply: %s" % fillmodel)
    res_r = apply(image, mdl_r, force_cpu=force_cpu, batch_size=batch_size,  volume_postprocessing=volume_postprocessing, noHU=noHU)
    spare_value = res_l.max()+1
    res_l[np.logical_and(res_l==0, res_r>0)] = spare_value
    res_l[res_r==0] = 0
    logging.info("Fusing results... this may take up to several minutes!")
    return utils.postrocessing(res_l, spare=[spare_value])
