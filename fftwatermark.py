import argparse
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter


def pil_resampling_lanczos():
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:
        return Image.LANCZOS


def load_font(font_path, size):
    if font_path:
        return ImageFont.truetype(font_path, size)
    # 注意：默认字体通常不支持中文，中文请传 --font
    return ImageFont.load_default()


def smoothstep(edge0, edge1, x):
    t = np.clip((x - edge0) / max(edge1 - edge0, 1e-8), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def hermitian_symmetrize_shifted(mask):
    """
    对 fftshift 后的频谱 mask 做中心对称化。
    对真实图像而言，FFT 幅度谱必须满足中心对称。
    """
    h, w = mask.shape
    cy, cx = h // 2, w // 2

    ii = (2 * cy - np.arange(h)) % h
    jj = (2 * cx - np.arange(w)) % w

    paired = mask[np.ix_(ii, jj)]
    return np.maximum(mask, paired)


def make_text_mask(
    h,
    w,
    text,
    font_path=None,
    font_scale=0.015,
    max_text_width=0.82,
    max_text_height=0.35,
    offset_x=0.0,
    offset_y=0.20,
    stroke=2,
    blur=1.2,
):
    """
    生成频域中的文字 mask。
    白色区域代表需要增强的 FFT 幅度。
    """
    canvas = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(canvas)

    # 自动根据图片大小和文字长度适配字号
    font_size = max(8, int(min(h, w) * font_scale))
    spacing = int(font_size * 0.18)

    for _ in range(40):
        font = load_font(font_path, font_size)
        bbox = draw.multiline_textbbox(
            (0, 0),
            text,
            font=font,
            spacing=spacing,
            stroke_width=stroke,
        )
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

        if tw <= w * max_text_width and th <= h * max_text_height:
            break

        font_size = int(font_size * 0.92)
        if font_size < 8:
            break

    font = load_font(font_path, font_size)
    bbox = draw.multiline_textbbox(
        (0, 0),
        text,
        font=font,
        spacing=spacing,
        stroke_width=stroke,
    )

    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    cx = w / 2 + offset_x * w
    cy = h / 2 + offset_y * h

    x = cx - tw / 2 - bbox[0]
    y = cy - th / 2 - bbox[1]

    draw.multiline_text(
        (x, y),
        text,
        fill=255,
        font=font,
        spacing=spacing,
        stroke_width=stroke,
        stroke_fill=255,
        align="center",
    )

    if blur > 0:
        canvas = canvas.filter(ImageFilter.GaussianBlur(radius=blur))

    arr = np.asarray(canvas).astype(np.float32) / 255.0
    if arr.max() > 0:
        arr /= arr.max()
    return arr


def make_image_mask(
    h,
    w,
    mask_path,
    invert=False,
    blur=1.0,
):
    """
    使用外部黑白图作为频域水印。
    要求：白色为水印内容，黑色为背景。
    """
    m = Image.open(mask_path).convert("L")
    m = m.resize((w, h), pil_resampling_lanczos())

    if blur > 0:
        m = m.filter(ImageFilter.GaussianBlur(radius=blur))

    arr = np.asarray(m).astype(np.float32) / 255.0
    if invert:
        arr = 1.0 - arr

    if arr.max() > 0:
        arr /= arr.max()
    return arr


def apply_frequency_band(mask, rmin=0.06, rmax=0.55, fade=0.04):
    """
    限制水印所在频率范围。

    rmin 太小：容易产生肉眼可见的低频明暗变化。
    rmax 太大：容易被 JPG 压缩抹掉。
    """
    h, w = mask.shape
    yy, xx = np.mgrid[0:h, 0:w]
    cy, cx = h // 2, w // 2

    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    rn = r / (min(h, w) / 2.0)

    low = smoothstep(rmin, rmin + fade, rn)
    high = 1.0 - smoothstep(rmax - fade, rmax, rn)
    band = low * high

    out = mask * band
    if out.max() > 0:
        out /= out.max()
    return out


def save_fft_preview(pil_img, path):
    """
    保存频谱预览图，方便检查水印是否可见。
    """
    y = np.asarray(pil_img.convert("YCbCr").split()[0]).astype(np.float32) / 255.0
    f = np.fft.fftshift(np.fft.fft2(y))
    mag = np.log1p(np.abs(f))

    lo, hi = np.percentile(mag, [2.0, 99.85])
    mag = np.clip((mag - lo) / max(hi - lo, 1e-8), 0, 1)

    preview = Image.fromarray(np.uint8(mag * 255), mode="L")
    preview.save(path)


def embed_fft_watermark(
    img,
    wm_mask,
    alpha=25,
    max_delta=20.0,
    clip_delta=10.0,
    percentile=75.0,
    limit_percentile=99.7,
):
    """
    在亮度 Y 通道中嵌入 FFT 幅度水印。

    alpha:
        频域增强强度，越大频谱中文字越明显，但越可能肉眼可见。

    max_delta:
        空间域亮度扰动的主要上限，单位为 8-bit 灰度级。
        例如 max_delta=4 表示绝大多数像素亮度变化控制在约 4/255 内。

    clip_delta:
        极端像素变化裁剪阈值，防止局部异常。
    """
    img = img.convert("RGB")
    ycbcr = img.convert("YCbCr")
    y, cb, cr = ycbcr.split()

    y_arr = np.asarray(y).astype(np.float32) / 255.0
    h, w = y_arr.shape

    # FFT
    f = np.fft.fftshift(np.fft.fft2(y_arr))
    mag = np.abs(f)
    phase = f / (mag + 1e-12)

    wm = wm_mask.astype(np.float32)
    wm = np.clip(wm, 0.0, 1.0)

    # 确保频域水印中心对称
    wm = hermitian_symmetrize_shifted(wm)

    # 选水印区域的参考幅度
    active = wm > 0.02
    if np.any(active):
        base = np.percentile(mag[active], percentile)
    else:
        base = np.percentile(mag, percentile)

    # 增强频谱幅度
    mag2 = mag + alpha * base * wm

    # 还原相位，逆变换
    f2 = mag2 * phase
    y2 = np.real(np.fft.ifft2(np.fft.ifftshift(f2))).astype(np.float32)

    diff = y2 - y_arr
    diff -= diff.mean()

    # 控制空间域扰动，保证肉眼不可见
    target = max_delta / 255.0
    p = np.percentile(np.abs(diff), limit_percentile)

    if p > target and p > 1e-12:
        diff *= target / p

    diff = np.clip(diff, -clip_delta / 255.0, clip_delta / 255.0)

    y_wm = np.clip(y_arr + diff, 0.0, 1.0)
    y_img = Image.fromarray(np.uint8(np.round(y_wm * 255)), mode="L")

    out = Image.merge("YCbCr", (y_img, cb, cr)).convert("RGB")
    return out


def save_image(img, path, jpg_quality=92):
    ext = os.path.splitext(path)[1].lower()

    if ext in [".jpg", ".jpeg"]:
        img.save(
            path,
            quality=jpg_quality,
            subsampling=0,
            optimize=True,
        )
    else:
        img.save(path)


def main():
    parser = argparse.ArgumentParser(
        description="给彩色图片添加 FFT 频谱文字/结构水印"
    )

    parser.add_argument("input", help="输入原图")
    parser.add_argument("output", help="输出图片，推荐 PNG 或高质量 JPG")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="要写入频谱的文字，例如 'NO AI'")
    group.add_argument("--mask", help="黑底白字/白图形的频域水印图片")

    parser.add_argument("--font", default=None, help="字体文件路径，中文必须指定")
    parser.add_argument("--font-scale", type=float, default=0.015, help="文字大小")
    parser.add_argument("--offset-x", type=float, default=0.0, help="频谱中文字水平偏移，单位为图宽比例")
    parser.add_argument("--offset-y", type=float, default=0.20, help="频谱中文字垂直偏移，单位为图高比例")
    parser.add_argument("--stroke", type=int, default=2, help="文字描边粗细")
    parser.add_argument("--blur", type=float, default=1.2, help="频域 mask 模糊半径")

    parser.add_argument("--invert-mask", action="store_true", help="反转外部 mask 黑白")

    parser.add_argument("--rmin", type=float, default=0.06, help="最低嵌入频率，过低可能肉眼可见")
    parser.add_argument("--rmax", type=float, default=0.55, help="最高嵌入频率，过高不抗 JPG")
    parser.add_argument("--fade", type=float, default=0.04, help="频带边缘渐变宽度")

    parser.add_argument("--alpha", type=float, default=25, help="频谱水印强度")
    parser.add_argument("--max-delta", type=float, default=20.0, help="主要亮度扰动上限，单位灰度级")
    parser.add_argument("--clip-delta", type=float, default=10.0, help="极端亮度扰动裁剪，单位灰度级")
    parser.add_argument("--jpg-quality", type=int, default=92, help="输出 JPG 质量")

    parser.add_argument("--save-preview", action="store_true", help="保存 FFT 频谱预览图")
    parser.add_argument("--jpeg-test-quality", type=int, default=82, help="额外生成 JPG 压缩测试质量")

    args = parser.parse_args()

    img = Image.open(args.input).convert("RGB")
    w, h = img.size

    if args.text is not None:
        wm = make_text_mask(
            h=h,
            w=w,
            text=args.text,
            font_path=args.font,
            font_scale=args.font_scale,
            offset_x=args.offset_x,
            offset_y=args.offset_y,
            stroke=args.stroke,
            blur=args.blur,
        )
    else:
        wm = make_image_mask(
            h=h,
            w=w,
            mask_path=args.mask,
            invert=args.invert_mask,
            blur=args.blur,
        )

    wm = apply_frequency_band(
        wm,
        rmin=args.rmin,
        rmax=args.rmax,
        fade=args.fade,
    )

    out = embed_fft_watermark(
        img,
        wm,
        alpha=args.alpha,
        max_delta=args.max_delta,
        clip_delta=args.clip_delta,
    )

    save_image(out, args.output, jpg_quality=args.jpg_quality)

    base, _ = os.path.splitext(args.output)

    if args.save_preview:
        save_fft_preview(img, base + "_original_fft.png")
        save_fft_preview(out, base + "_watermarked_fft.png")

        # 额外模拟一次 JPG 压缩，检查抗压缩效果
        jpg_test = base + f"_jpeg_q{args.jpeg_test_quality}.jpg"
        out.save(
            jpg_test,
            quality=args.jpeg_test_quality,
            subsampling=0,
            optimize=True,
        )
        jpg_img = Image.open(jpg_test).convert("RGB")
        save_fft_preview(jpg_img, base + f"_jpeg_q{args.jpeg_test_quality}_fft.png")

    print("完成：", args.output)

    if args.save_preview:
        print("已保存频谱预览：")
        print(" -", base + "_original_fft.png")
        print(" -", base + "_watermarked_fft.png")
        print(" -", base + f"_jpeg_q{args.jpeg_test_quality}_fft.png")


if __name__ == "__main__":
    main()
