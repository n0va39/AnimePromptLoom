"""Per-character settings and separation of private labels from prompt names."""
import copy
import re
import uuid

ORDINALS = ("첫 번째", "두 번째", "세 번째", "네 번째", "다섯 번째", "여섯 번째",
            "일곱 번째", "여덟 번째", "아홉 번째", "열 번째", "열한 번째", "열두 번째")


def validate_profiles(value):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("캐릭터는 최대 12명까지 별도로 설정할 수 있습니다.")
    profiles, ids, labels = [], set(), set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("캐릭터 설정 형식이 올바르지 않습니다.")
        profile = {}
        for key, limit in (("id", 64), ("label", 100), ("name", 200), ("designation", 200), ("description", 6000)):
            text = item.get(key, "")
            if not isinstance(text, str) or len(text) > limit:
                raise ValueError(f"캐릭터 {key} 입력은 {limit:,}자 이하여야 합니다.")
            if key != "description" and any(c in text for c in "\r\n`"):
                raise ValueError("이름과 지칭은 코드 블록 없는 한 줄로 입력해 주세요.")
            profile[key] = text.strip()
        profile["id"] = profile["id"] or uuid.uuid4().hex
        if profile["id"] in ids:
            raise ValueError("캐릭터 ID가 중복되었습니다.")
        ids.add(profile["id"])
        label = profile["label"].casefold()
        if label and label in labels:
            raise ValueError("인물 구분용 이름은 캐릭터마다 다르게 입력해 주세요.")
        labels.add(label)
        profile["use_name"] = item.get("use_name", False)
        if type(profile["use_name"]) is not bool:
            raise ValueError("이름 사용 설정이 올바르지 않습니다.")
        if profile["use_name"] and not profile["name"]:
            raise ValueError("이름 사용을 선택했다면 출력할 이름·태그·LoRA 트리거를 입력해 주세요.")
        profiles.append(profile)
    return profiles


def legacy_profiles(session):
    """Keep ambiguous old prose intact in one card; never guess its characters."""
    text = session.get("characters", "").strip()
    if not text:
        return []
    match = re.match(r"^([^\n:：]{1,40})[:：]\s*(.+)$", text, re.S)
    return [{"id": uuid.uuid5(uuid.NAMESPACE_URL, session["id"] + "/legacy-character").hex,
             "label": match[1].strip() if match else "", "name": "", "use_name": False,
             "designation": "", "description": match[2] if match else text}]


def reference(index):
    return f"Character {index + 1}"


def mask_labels(text, profiles):
    replacements = {}
    for index, profile in enumerate(profiles):
        for value in (profile["label"], profile["name"]):
            if value and (value == profile["label"] or not profile["use_name"]):
                replacements[value] = reference(index)
    if not replacements:
        return text
    patterns = []
    for value in sorted(replacements, key=len, reverse=True):
        # Korean postpositions may follow names directly; Latin names must not
        # match pieces of ordinary words (e.g. Ann in anniversary).
        patterns.append((r"(?<![A-Za-z0-9_])" if value[0].isascii() and value[0].isalnum() else "")
                        + re.escape(value)
                        + (r"(?![A-Za-z0-9_])" if value[-1].isascii() and value[-1].isalnum() else ""))
    lookup = {key.casefold(): value for key, value in replacements.items()}
    return re.sub("|".join(patterns), lambda match: lookup[match[0].casefold()], text, flags=re.I)


def model_profiles(profiles):
    return [{"profile_id": profile["id"], "reference": reference(index),
             "designation": profile["designation"] or f"{ORDINALS[index]} 캐릭터",
             "use_name": profile["use_name"], "prompt_name": profile["name"] if profile["use_name"] else "",
             "description": mask_labels(profile["description"], profiles)} for index, profile in enumerate(profiles)]


NAME_POLICY = (
    "\n캐릭터 지칭 규칙: reference는 내부 구분용이며 고유 이름이 아니다. 각 인물의 profile_id를 응답 characters 항목에 정확히 붙인다. "
    "등록되지 않은 추가 인물은 profile_id를 빈 문자열로 둔다. 현재 장면에 등장한 인물만 출력한다. "
    "use_name=false이면 고유 이름을 만들거나 음역하지 말고 designation을 자연스러운 영문 지칭으로 풀어 쓴다. "
    "예: 첫 번째 여성 -> the first woman, 왼쪽의 사람 -> the person on the left. "
    "designation이 기본 '첫 번째 캐릭터'라면 그 인물의 설정에 맞는 '첫 번째 여성' 등도 같은 인물의 지칭이다. "
    "순번은 인물을 구분하는 순서이며 좌우 위치나 구도를 뜻하지 않는다. 좌우는 designation에 명시됐을 때만 반영한다. "
    "use_name=true이면 prompt_name을 번역하거나 대소문자·underscore를 바꾸지 않고 그대로 사용한다. "
    "이름으로 외형을 추측하지 말고 해당 인물의 description을 유지한다. 임의 고유 이름은 생성하지 않는다."
    "\n출력 계약: 각 characters 항목에 designation_en(자연스러운 영문 지칭)과 designation_ko(같은 의미의 한국어 지칭)를 작성한다. "
    "use_name=true이면 둘 다 prompt_name 그대로다. use_name=false이면 designation을 해석한다. "
    "전체 description과 translation 및 인물별 description에서는 등록 인물의 지칭 대신 [[Character 1]] 같은 reference 표식을 사용한다. "
    "예: [[Character 1]] looks at [[Character 2]]. 표식은 프로그램이 번역된 지칭으로 치환한다. "
    "전체 description과 translation에 등장 인물 각각의 표식을 적어도 한 번 포함한다. "
    "표식·profile_id·designation은 태그가 아니므로 tags에 넣지 않는다. tags에는 실제 시각적 속성 태그만 적는다."
)


def generation_schema(base, profiles):
    schema = copy.deepcopy(base)
    if profiles:
        character = schema["properties"]["characters"]["items"]
        character["properties"]["profile_id"] = {"type": "string", "enum": [""] + [p["id"] for p in profiles]}
        for key in ("designation_en", "designation_ko"):
            character["properties"][key] = {"type": "string"}
        character["required"] += ["profile_id", "designation_en", "designation_ko"]
    return schema


def render_references(doc, profiles):
    """The LLM translates free-form designations; code binds them to stable people."""
    if not profiles:
        return
    indexed = {p["id"]: (index, p) for index, p in enumerate(profiles)}
    replacements, seen = {}, set()
    for character in doc["characters"]:
        profile_id = character.get("profile_id", "")
        if not profile_id:
            continue
        if profile_id not in indexed or profile_id in seen:
            raise ValueError("LLM의 캐릭터 설정 연결이 올바르지 않습니다. 다시 생성해 주세요.")
        seen.add(profile_id)
        index, profile = indexed[profile_id]
        marker = f"[[{reference(index)}]]"
        en, ko = character.get("designation_en"), character.get("designation_ko")
        if profile["use_name"]:
            en = ko = profile["name"]
        if not all(isinstance(value, str) and value.strip() and "[[" not in value for value in (en, ko)):
            raise ValueError("LLM의 대체 지칭 번역이 비어 있습니다. 다시 생성해 주세요.")
        for key, value in (("description", en.strip()), ("translation", ko.strip())):
            # Qwen sometimes expands a reference itself. Accept it only when
            # the exact chosen name/designation is already present.
            direct = value in doc[key] if profile["use_name"] else value.casefold() in doc[key].casefold()
            if marker not in doc[key] and not direct:
                raise ValueError("LLM이 캐릭터 지칭을 묘사에서 누락했습니다. 다시 생성해 주세요.")
        replacements[marker] = (en.strip(), ko.strip(), profile["use_name"])

    def render(text, korean=False):
        def replace(match):
            if match[0] not in replacements:
                raise ValueError("LLM이 설정과 연결되지 않은 인물 지칭을 사용했습니다. 다시 생성해 주세요.")
            en, ko, exact = replacements[match[0]]
            value = ko if korean else en
            prefix = text[:match.start()].rstrip()
            if not korean and not exact and (not prefix or prefix[-1] in ".!?"):
                value = value[0].upper() + value[1:]
            return value
        return re.sub(r"\[\[Character \d+\]\]", replace, text)

    doc["description"] = render(doc["description"])
    doc["translation"] = render(doc["translation"], korean=True)
    for character in doc["characters"]:
        character["description"] = render(character["description"])
