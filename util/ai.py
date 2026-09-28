import json
import os

from openai import OpenAI
from config import ai_key
from util.enncy import search
from util.ocr import ocr_form_url_image

### 摘抄自一个学弟的第二课堂仓库AI部分
## https://github.com/tinyvan/SecondClass

# Any OpenAI-compatible endpoint works: set AI_BASE_URL / AI_MODEL / AI_KEY.
# Defaults keep the original ChatAnywhere behavior when env vars are absent.
ai_base_url = os.getenv("AI_BASE_URL", "https://api.chatanywhere.tech/v1")
ai_model = os.getenv("AI_MODEL", "gpt-4o-mini")
# Safety-net cap for subjective/fill-blank answers: the prompt asks for a
# concise answer (a few sentences), this only guards against runaway
# model output, so it is generous rather than a strict limit.
SUBJECTIVE_MAX_CHARS = int(os.getenv("SUBJECTIVE_MAX_CHARS", "200"))
# Optional: disable the enncy question-bank lookup (empty key = skip).
enncy_enabled = bool(os.getenv("ENNCY_KEY", ""))

system_prompt = """
我将为你发送类似如下格式的文本，question为使用OCR工具对图片题目识别的结果，你需要根据语义拼接，有的时候ABCD字符会缺失或者在选项后面，具体根据顺序和语义；
searched为尝试将question提交到题库API进行检索的结果，仅用于当你无法判断出答案时，用于辅助，不一定与题目契合！；
options提供选项（如果选项为空，请从question中寻找，如果question没有选项，只能蒙一个answers了），回答的时候必须根据type（例如type为单选题应该只给一个结果，type为填空题应该给文本，type为多选题应该给多个答案）
{
    "type": "单选题",
    "question": ['下面（）算法适合构造一个稠密图G的最小生成树', 'A.Prim算法', 'B.Kruskal算法', 'C.Floyd算法', 'D.Dijkstra算法'],
    "options": ["A","B","C","D"],
    "searched" : "题库搜索结果"
}
你应该回答：
{
"thinking":"你简洁的思考过程",
"answer":["A"]
}
如果question模糊不清，即使结合JSON的所有信息都无法辨别并给出答案。answer置为空list
当type为主观题或填空题时，answer为文本数组：answer的每个元素必须简洁、直接给出答案本身，不要冗长的解释、铺垫或markdown格式；填空题每个元素对应一个空（按顺序）；主观题通常只有一个元素；主观题回答保持简短，几句话以内，能列点就列点（用顿号或分号分隔要点）
"""

client = None


def LLM_init(api_key: str):
    global client
    if client is None:
        client = OpenAI(
            api_key=api_key,
            base_url=ai_base_url,
        )
    return client


def get_ans(text):
    if client is None:
        raise Exception("LLM is not initialized")
    completion = client.chat.completions.create(
        model=ai_model,
        response_format={"type": "json_object"},
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': text}],
    )
    return completion.choices[0].message.content


def _option_text(option):
    """Extract the answer text of a choice option. Only `value` counts:
    `key` is the always-present option letter (A/B/C/D), so falling back to
    it would make blank-option detection impossible."""
    if isinstance(option, dict):
        return str(option.get("value") or "")
    return str(getattr(option, "value", None) or "")


def request_ai(type, problem, options, img_url):
    problem_text = problem
    # OCR when the question references an image (image-only questions, or
    # choice questions whose option text lives in the slide image).
    options_all_blank = (isinstance(options, list) and bool(options)
                         and all(not _option_text(o).strip() for o in options))
    needs_image = problem == "" or options_all_blank or "如图" in str(problem)
    if img_url and needs_image:
        print("题目依赖图片 启用OCR识别", flush=True)
        ocr_text = ocr_form_url_image(img_url)
        if ocr_text:
            print("OCR识别结果", ocr_text, flush=True)
            # OCR text supplements the original body; image-only questions
            # may have no body at all.
            problem_text = f"{problem}\n{ocr_text}".strip() if problem else ocr_text
        else:
            print("OCR未识别到文字", flush=True)

    LLM_init(ai_key)
    send = {
        "type": type,
        "question": problem_text,
        "options": options
    }

    if enncy_enabled:
        enncy_result = search(problem_text)
        print("搜题结果", enncy_result)
        send["searched"] = enncy_result

    response = get_ans(str(send))
    print(response)
    answer = json.loads(response).get("answer", [])
    if not isinstance(answer, list):
        answer = [answer]
    answer = [str(item) for item in answer]

    # Safety net for subjective/fill-blank answers: the prompt already asks
    # for a concise answer; this only kicks in on runaway output (200+ chars)
    # so submissions stay a reasonable length.
    if type in ("主观题", "填空题"):
        capped = []
        for item in answer:
            if len(item) > SUBJECTIVE_MAX_CHARS:
                # cut at the last sentence boundary within the limit, else hard cut
                cut = item[:SUBJECTIVE_MAX_CHARS]
                for sep in ("。", "；", ";", "！", "？"):
                    idx = cut.rfind(sep)
                    if idx >= int(SUBJECTIVE_MAX_CHARS * 0.5):
                        cut = cut[:idx + 1]
                        break
                capped.append(cut)
            else:
                capped.append(item)
        answer = capped
    return answer
