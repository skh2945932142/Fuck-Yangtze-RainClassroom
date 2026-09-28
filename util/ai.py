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


def request_ai(type, problem, options, img_url):
    problem_text = problem
    if problem == "":
        print("题目文本为空 启用OCR图片识别")
        problem_text = ocr_form_url_image(img_url)
        print("OCR识别结果", problem_text)

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
    return answer if isinstance(answer, list) else [answer]
