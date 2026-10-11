# 로컬 태그 CSV 준비

CSV는 이 저장소에 포함하지 않습니다. 사용할 수 있는 데이터의 이용 조건을 확인한 뒤 `data/danbooru_tags_classified.csv`에 두거나 `python app.py --csv "path/to/tags.csv"`로 지정하세요. 자동 다운로드는 하지 않습니다. CSV가 없으면 앱에서 준비 안내를 표시하고 생성을 비활성화합니다.

UTF-8(또는 UTF-8 BOM), 첫 행은 다음 헤더가 필요합니다.

```csv
name,category,post_count,description
```

각 데이터 행은 영문 태그명, 정수 분류, 정수 사용 빈도, 한국어 설명의 4열입니다. 선택적인 다섯 번째 별칭 열도 읽을 수 있지만 별칭을 공식 태그로 승인하지 않습니다. 설명에 쉼표가 있으면 CSV 규칙에 따라 따옴표로 감싸세요.

## 개발 검증 사본의 출처

- 파일: `danbooru_tags_classified.csv`
- 복사일: 2026-10-09 (Asia/Seoul)
- 참조 파일: EasyUseAnima의 `__easyuse_anima__/danbooru_tags_classified.csv`
- EasyUseAnima의 `autocomplete_dataset.py`가 사용하는 기본 CSV와 동일한 사본입니다.
- upstream: https://github.com/Localsmile/danbooru_KR_wiki_tag_search
- SHA256: `775b0abbad1fe75c9d9f8bc6bf8e516a368bd6c1f71dd78aa53798fe24cd671d`
- 전체 고유 태그: 114,092개. 한국어 문자가 있는 설명: 71,179개.
- 원본 바이트를 그대로 복사했으며 자동 갱신하지 않습니다. 이 파일은 사용자의 기존 로컬 데이터를 참조하기 위한 사본입니다. 재배포 권한을 별도로 검증한 것은 아닙니다.
- 4열(태그/분류/빈도/설명)과 5열(추가 별칭) 행이 혼재합니다. 모든 태그명을 읽되 별칭 열은 공식 태그명으로 승인하지 않습니다.
- 2026-10-11 확인: upstream 루트와 README에서 배포 라이선스를 찾지 못했고 GitHub의 license 메타데이터도 null이었습니다. 명확한 재배포 조건을 확인할 때까지 CSV 및 여기서 복사한 설명 데이터를 커밋하지 않습니다. 원본이 공개되어 있다는 사실만으로 재배포 허용으로 판단하지 않습니다.
