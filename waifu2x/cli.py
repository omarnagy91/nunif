# waifu2x
import os
from os import path
import torch
from torchvision.transforms import (
    functional as TF,
    InterpolationMode)
from PIL import Image
import argparse
import csv
from tqdm import tqdm
import mimetypes
from multiprocessing import cpu_count
from concurrent.futures import ThreadPoolExecutor as PoolExecutor
from nunif.logger import logger
from nunif.utils.image_loader import ImageLoader
from nunif.utils.filename import set_image_ext
from nunif.utils import video as VU
from .utils import Waifu2x
from .download_models import main as download_main


# Add missing webp mimetype
mimetypes.add_type("image/webp", ".webp")


DEFAULT_ART_MODEL_DIR = path.abspath(path.join(
    path.join(path.dirname(path.abspath(__file__)), "pretrained_models"),
    "swin_unet", "art"))

DEFAULT_ART_SCAN_MODEL_DIR = path.abspath(path.join(
    path.join(path.dirname(path.abspath(__file__)), "pretrained_models"),
    "swin_unet", "art_scan"))

DEFAULT_PHOTO_MODEL_DIR = path.abspath(path.join(
    path.join(path.dirname(path.abspath(__file__)), "pretrained_models"),
    "swin_unet", "photo"))


def find_subdir(dirname):
    subdirs = [f.path for f in os.scandir(dirname) if f.is_dir()]
    for dirname in list(subdirs):
        subdirs.extend(find_subdir(dirname))
    return subdirs


def is_image(filename, mime=None):
    mime = mime or mimetypes.guess_type(filename)[0]
    return mime and mime.startswith("image")


def is_video(filename, mime=None):
    mime = mime or mimetypes.guess_type(filename)[0]
    return mime and mime.startswith("video")


def is_text(filename, mime=None):
    mime = mime or mimetypes.guess_type(filename)[0]
    return mime and mime.startswith("text")


def is_output_dir(filename):
    return path.isdir(filename) or "." not in path.basename(filename)


@torch.inference_mode()
def process_image(ctx, im, meta, args):
    rgb, alpha = IL.to_tensor(im, return_alpha=True)
    rgb, alpha = ctx.convert(
        rgb, alpha, args.method, args.noise_level,
        args.tile_size, args.batch_size,
        args.tta, enable_amp=not args.disable_amp)
    if args.depth is not None:
        meta["depth"] = args.depth
    depth = meta["depth"] if "depth" in meta and meta["depth"] is not None else 8
    if args.grayscale:
        meta["grayscale"] = True

    return IL.to_image(rgb, alpha, depth=depth)


def process_images(ctx, files, output_dir, args):
    os.makedirs(output_dir, exist_ok=True)
    loader = ImageLoader(files=files, max_queue_size=128,
                         load_func=IL.load_image,
                         load_func_kwargs={"color": "rgb", "keep_alpha": True})
    futures = []
    with PoolExecutor(max_workers=cpu_count() // 2 or 1) as pool:
        for im, meta in tqdm(loader, ncols=60):
            output_filename = path.join(
                output_dir,
                set_image_ext(path.basename(meta["filename"]), format=args.format))
            if args.resume and path.exists(output_filename):
                continue
            output = process_image(ctx, im, meta, args)
            futures.append(pool.submit(
                IL.save_image, output,
                filename=output_filename,
                meta=meta, format=args.format))
        for f in futures:
            f.result()


def process_video(ctx, input_filename, args):
    def config_callback(stream):
        fps = VU.get_fps(stream)
        if float(fps) > args.max_fps:
            fps = args.max_fps

        options = {"preset": args.preset, "crf": str(args.crf)}
        tune = []
        if fps < 2:
            tune += ["stillimage"]
        if args.grain:
            tune += ["grain"]
        if args.tune:
            tune += args.tune
        tune = set(tune)
        if tune:
            options["tune"] = ",".join(tune)
        return VU.VideoOutputConfig(
            fps=fps,
            pix_fmt=args.pix_fmt,
            options=options
        )

    def frame_callback(frame):
        im = frame.to_image()
        if args.rotate_left:
            im = im.transpose(Image.Transpose.ROTATE_90)
        elif args.rotate_right:
            im = im.transpose(Image.Transpose.ROTATE_270)

        rgb = TF.to_tensor(im)
        with torch.inference_mode():
            output, _ = ctx.convert(
                rgb, None, args.method, args.noise_level,
                args.tile_size, args.batch_size,
                args.tta, enable_amp=not args.disable_amp)
        if args.grain:
            noise = (torch.randn(output.shape) +
                     TF.resize(torch.randn((3, output.shape[1] // 2, output.shape[2] // 2)),
                               (output.shape[1], output.shape[2]),
                               interpolation=InterpolationMode.NEAREST))
            correlated_noise = noise * output
            light_decay = (1. - output.mean(dim=0, keepdim=True)) ** 2
            output = output + correlated_noise * light_decay * args.grain_strength
            output = torch.clamp(output, 0, 1)
        return frame.from_image(TF.to_pil_image(output))

    if is_output_dir(args.output):
        os.makedirs(args.output, exist_ok=True)
        output_filename = path.join(
            args.output,
            path.splitext(path.basename(input_filename))[0] + ".mp4")
    else:
        output_filename = args.output

    if args.resume and path.exists(output_filename):
        return

    if not args.yes and path.exists(output_filename):
        y = input(f"File '{output_filename}' already exists. Overwrite? [y/N]").lower()
        if y not in {"y", "ye", "yes"}:
            return

    VU.process_video(input_filename, output_filename,
                     config_callback=config_callback,
                     frame_callback=frame_callback,
                     vf=args.vf)


def process_file(ctx, input_filename, args):
    if is_video(input_filename):
        process_video(ctx, input_filename, args)
    elif is_image(input_filename):
        if is_output_dir(args.output):
            os.makedirs(args.output, exist_ok=True)
            fmt = args.format
            output_filename = path.join(
                args.output,
                set_image_ext(path.basename(args.input), format=fmt))
        else:
            _, ext = path.splitext(input_filename)
            fmt = ext.lower()[1:]
            if fmt not in {"png", "webp", "jpeg", "jpg"}:
                raise ValueError(f"Unable to recognize image extension: {fmt}")
            output_filename = args.output
        if args.resume and path.exists(output_filename):
            return
        im, meta = IL.load_image(input_filename, color="rgb", keep_alpha=True)
        output = process_image(ctx, im, meta, args)
        IL.save_image(output, filename=output_filename, meta=meta, format=fmt)
    elif is_text(input_filename):
        files = load_files(input_filename)
        image_files = [f for f in files if is_image(f)]
        if image_files:
            process_images(ctx, image_files, args.output, args)
        video_files = [f for f in files if is_video(f)]
        for video_file in video_files:
            process_video(ctx, video_file, args)


def load_files(txt):
    files = []
    with open(txt, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            files.append(row[0])
    return files


def main(args):
    if args.model_dir is None:
        if args.style == "photo":
            model_dir = DEFAULT_PHOTO_MODEL_DIR
        elif args.style in {"scan", "art_scan"}:
            model_dir = DEFAULT_ART_SCAN_MODEL_DIR
        else:
            model_dir = DEFAULT_ART_MODEL_DIR
    else:
        model_dir = args.model_dir

    ctx = Waifu2x(model_dir=model_dir, gpus=args.gpu)
    ctx.load_model(args.method, args.noise_level)

    if path.isdir(args.input):
        if args.recursive:
            subdirs = sorted([args.input] + find_subdir(args.input))
            for input_dir in subdirs:
                files = ImageLoader.listdir(input_dir)
                if not files:
                    continue
                print(f"* {input_dir}")
                output_dir = path.normpath(path.join(args.output, path.relpath(input_dir, start=args.input)))
                process_images(ctx, files, output_dir, args)
        else:
            process_images(ctx, ImageLoader.listdir(args.input), args.output, args)
    else:
        process_file(ctx, args.input, args)


if __name__ == "__main__":
    default_gpu = 0 if torch.cuda.is_available() or torch.backends.mps.is_available() else -1
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--model-dir", type=str, help="model dir")
    parser.add_argument("--noise-level", "-n", type=int, default=0, choices=[0, 1, 2, 3], help="noise level")
    parser.add_argument("--method", "-m", type=str,
                        choices=["scale4x", "scale2x",
                                 "noise_scale4x", "noise_scale2x",
                                 "scale", "noise", "noise_scale"],
                        default="noise_scale", help="method")
    parser.add_argument("--gpu", "-g", type=int, nargs="+", default=[default_gpu],
                        help="GPU device ids. -1 for CPU")
    parser.add_argument("--batch-size", type=int, default=4,
                        help="minibatch_size")
    parser.add_argument("--tile-size", type=int, default=256,
                        help="tile size for tiled render")
    parser.add_argument("--output", "-o", type=str, required=True,
                        help="output file or directory")
    parser.add_argument("--input", "-i", type=str, required=True,
                        help="input file or directory. (*.txt, *.csv) for image list")
    parser.add_argument("--tta", action="store_true", help="use TTA mode")
    parser.add_argument("--disable-amp", action="store_true", help="disable AMP for some special reason")
    parser.add_argument("--image-lib", type=str, choices=["pil", "wand"], default="pil",
                        help="image library to encode/decode images")
    parser.add_argument("--depth", type=int,
                        help="bit-depth of output image. enabled only with `--image-lib wand`")
    parser.add_argument("--format", "-f", type=str, default="png", choices=["png", "webp", "jpeg"],
                        help="output image format")
    parser.add_argument("--style", type=str, choices=["art", "photo", "scan", "art_scan"],
                        help=("style for default model (art/scan/photo). "
                              "Ignored when --model-dir option is specified."))
    parser.add_argument("--grayscale", action="store_true",
                        help="Convert to grayscale format")
    parser.add_argument("--recursive", "-r", action="store_true",
                        help="process all subdirectories")
    parser.add_argument("--resume", action="store_true",
                        help="skip processing when output file is already exist")
    parser.add_argument("--max-fps", type=float, default=128,
                        help="max framerate. output fps = min(fps, --max-fps) (video only)")
    parser.add_argument("--crf", type=int, default=20,
                        help="constant quality value. smaller value is higher quality (video only)")
    parser.add_argument("--preset", type=str, default="ultrafast",
                        choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                                 "medium", "slow", "slower", "veryslow", "placebo"],
                        help="encoder preset option (video only)")
    parser.add_argument("--tune", type=str, nargs="+", default=["zerolatency"],
                        choices=["film", "animation", "grain", "stillimage",
                                 "fastdecode", "zerolatency"],
                        help="encoder tunings option (video only)")
    parser.add_argument("--yes", "-y", action="store_true", default=False,
                        help="overwrite output files (video only)")
    parser.add_argument("--rotate-left", action="store_true",
                        help="Rotate 90 degrees to the left(counterclockwise) (video only)")
    parser.add_argument("--rotate-right", action="store_true",
                        help="Rotate 90 degrees to the right(clockwise) (video only)")
    parser.add_argument("--vf", type=str, default="",
                        help="video filter options for ffmpeg. (video only)")
    parser.add_argument("--grain", action="store_true",
                        help=("add noise after denosing (video only)"))
    parser.add_argument("--grain-strength", type=float, default=0.05,
                        help=("noise strength  (video only)"))
    parser.add_argument("--pix-fmt", type=str, default="yuv420p", choices=["yuv420p", "yuv444p"],
                        help=("pixel format (video only)"))

    args = parser.parse_args()
    logger.debug(f"waifu2x.cli.main: {str(args)}")
    if args.image_lib == "wand":
        from nunif.utils import wand_io as IL
    else:
        from nunif.utils import pil_io as IL

    # alias for typo
    if args.method == "scale2x":
        args.method = "scale"
    elif args.method == "noise_scale2x":
        args.method = "noise_scale"

    # download models
    pretrained_model_dir = path.join(path.dirname(__file__), "pretrained_models")
    if not path.exists(pretrained_model_dir):
        download_main()

    # main
    main(args)
