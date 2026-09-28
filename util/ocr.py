import cv2
import numpy as np
import requests

# Lazy init: importing PaddleOCR at module load takes ~1GB RAM and several
# seconds, which is unacceptable in a long-running loop that rarely needs OCR.
ocr = None


def _get_ocr():
    global ocr
    if ocr is None:
        from paddleocr import PaddleOCR
        ocr = PaddleOCR(use_angle_cls=False, lang="ch")
    return ocr

def ocr_form_url_image(url: str):
    """识别网络图片"""
    try:
        response = requests.get(url)
        response.raise_for_status()
        image_array = np.asarray(bytearray(response.content), dtype=np.uint8)
        img = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        return get_ocr_result(_get_ocr().ocr(img))
    except Exception as e:
        print(f"下载或识别失败: {e}")
        return None


def get_ocr_result(result):
    if result is None:
        return list()
    try:
        return dict(list(result)[0])["rec_texts"]
    except Exception as e:
        print(f"未识别到文字: {e}")
        return list()


# r = ocr_form_url_image("https://changjiang-private-qn.yuketang.cn/slide/19860393/cover855_20250522155850.jpg?e=1759671799&token=IAM-gs8ue1pDIGwtR1CR0Zjdagg7Q2tn5G_1BqTmhmqa:kTHXesZisVgbXKq2NjE3WcC02G8=")
# print(r)
