import os
from PIL import Image, ImageEnhance


def preprocess_image(file_path, save_path=None):
    """
    save_path — куда положить результат. Сервис разбора передаёт файл внутри
    папки своего разбора; без параметра — прежний общий data/temp_proc.png.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(base_dir, "data")

    if save_path is None and not os.path.exists(data_dir):
        os.makedirs(data_dir)

    img = Image.open(file_path)

    img = img.convert("L")

    img = ImageEnhance.Contrast(img).enhance(3.0)

    img = ImageEnhance.Sharpness(img).enhance(2.0)


    threshold = 140
    img = img.point(lambda p: 255 if p > threshold else 0)
    img = img.convert("L")

    if save_path is None:
        save_path = os.path.join(data_dir, "temp_proc.png")
    img.save(save_path)
    return save_path
