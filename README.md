# Lofin365 정책 크롤러 (정책 수집 -> 상세 추출 -> Markdown/XLSX + 검색 색인 저장)

중앙정부 정책 ID v2의 식별 기준·기존 ID 호환성·수동 재색인 절차는
[정책 ID 전환 안내](docs/policy-id-v2.md)를 참고하세요. 지방정부 ID는 유지합니다.

## 1) 설치
```bash
cd lofin-crawler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 1-1) `.env` 설정
웹 UI 세션 키 등 선택 환경변수는 프로젝트 루트의 `.env`에 둘 수 있습니다.
`crawl_lofin.py`, `webapp.py` 실행 시 `.env`가 자동 로드됩니다.

## 2) CLI 실행 예시 (인구 정책)
```bash
python crawl_lofin.py --keyword 인구 --limit 50 --output population_policies.md --headed --slow-mo 200
```

검색 앱에서 사용하려면 같은 이름의 `.json` 색인을 함께 사용하세요.
아래 명령은 `population_policies_YYYYMMDD_HHMMSS.md`와 `population_policies_YYYYMMDD_HHMMSS.json`을 함께 생성합니다.

```bash
python crawl_lofin.py --keyword 인구 --limit 50 --output population_policies.md
```

XLSX도 필요하면 `--format xlsx` 또는 `--format both`를 사용합니다.

```bash
python crawl_lofin.py --keyword 인구 --limit 50 --format both --output population_policies.md
```

## 2-1) 연도/지역 필터와 함께 실행
```bash
python crawl_lofin.py --keyword 인구 --year 2026-01-01 --region 서울 --limit 50 --output population_policies.md --headed
```

검색어를 비우면 연도/지역 조건 기준으로 전체 조회를 시도합니다.
CLI/웹 UI 입력 상한은 최대 500,000건입니다. 대량 수집은 대상 사이트 응답속도와 상세 페이지 수집 시간에 따라 오래 걸릴 수 있습니다.
`--limit`이 `--chunk-size`보다 크면 기본적으로 10,000건 단위 part 파일과 manifest를 생성합니다.

```bash
python crawl_lofin.py --year 2026-01-01 --limit 500000 --chunk-size 10000 --output "2026 전국 정책.md"
```

## 2-2) 대량(6천 건+) 수집 권장 옵션
상세 페이지는 기본적으로 `popup API 우선 + HTTP 상세 페이지 fallback`을 병렬로 호출합니다.

```bash
python crawl_lofin.py \
  --keyword 인구 \
  --limit 6000 \
  --detail-request-timeout-ms 7000 \
  --detail-concurrency 24 \
  --detail-batch-size 1000 \
  --detail-runtime-timeout-ms 800 \
  --detail-delay-ms 20 \
  --output population_policies_6000.md
```

- `--detail-concurrency`: 상세 HTTP 요청 병렬 처리 수. 기본값은 24입니다. 높일수록 빠르지만 대상 서버 오류율과 부하가 증가할 수 있습니다.
- `--detail-batch-size`: 상세 병렬 수집 전에 모을 목록 건수. 기본값은 1000입니다. 크게 잡을수록 페이지 이동 대기 비중이 줄지만 진행률 갱신 간격이 길어집니다.
- `--detail-delay-ms`: 건별 지연(ms). 낮출수록 빠르지만 대상 서버 부하가 증가합니다.
- `--detail-request-timeout-ms`: 상세 HTTP 요청 타임아웃(ms).
- `--detail-runtime-timeout-ms`: 목록 구조를 읽지 못해 브라우저 클릭 fallback을 사용할 때의 런타임 대기(ms).
- `--chunk-size`: part 파일당 저장 건수. `0`이면 기존처럼 단일 파일로 저장합니다.

## 2-3) 중앙정부(열린재정) 목록 수집
중앙정부 `재정사업별 설명자료` 목록을 수집하려면 `--source central`을 사용합니다.
`세부사업명` 상세팝업에서 다운로드되는 HWPX를 읽어 `사업내용` 컬럼을 가능한 경우 자동 보강합니다.
예산안/예산총괄표는 현재 제외합니다.

```bash
python crawl_lofin.py \
  --source central \
  --year 2026 \
  --limit 200 \
  --output central_2026.md
```

- `--year`는 `2026` 또는 `2026-01-01` 모두 사용 가능합니다.
- 중앙정부 회계년은 `2018~2026` 범위만 지원합니다.
- `--keyword`를 비우면 해당 회계년 전체 조회를 시도합니다.

## 3) HTML 웹에서 실행
```bash
python webapp.py
```
- 브라우저에서 `http://127.0.0.1:8787` 접속
- 폼에서 `검색어/출처/날짜(또는 회계년)/지역/갯수/청크 크기/파일 형식` 입력 후 실행
- 검색어를 비우면 선택한 조건 기준으로 전체 조회를 시도합니다.
- `출처=중앙정부` 선택 시 `회계년(2018~2026)`만 표시되고 `지역` 입력은 숨김 처리됩니다.
- 결과 파일은 프로젝트 폴더에 `*_YYYYMMDD_HHMMSS.md`와 `*_YYYYMMDD_HHMMSS.json`으로 생성
- `파일 형식=Markdown + XLSX`를 고르면 `.md`, `.xlsx`, `.json`을 함께 생성
- 대량 수집 시 `*_part001.md`, `*_part001.json`, `*_manifest.json` 형식으로 나뉘어 생성

### 3-1) Windows 더블클릭 실행
- `start_webapp.bat` 더블클릭
- 자동으로 서버 실행 + 브라우저(`http://127.0.0.1:8787`) 오픈

## 4) 결과 컬럼
- `번호`, `사업명`, `지역`, `자치단체`, `회계연도`, `조직`, `사업목적`, `사업기간`, `총사업비`, `사업내용`, `지원형태`, `지원조건`, `사업위치`, `시행주체`, `추진근거`, `추진경위`, `추진계획`

중앙정부(`--source central`) 결과 컬럼:
- `번호`, `회계연도`, `소관명`, `회계명`, `분야명`, `부문명`, `프로그램명`, `단위사업명`, `세부사업명`, `예산액`, `사업내용(가능 시)`

## 5) 셀렉터 조정이 필요한 경우
사이트 구조 변경 시 `crawl_lofin.py` 상단의 아래 목록을 수정:
- `YEAR_FILTER_SELECTORS`
- `REGION_FILTER_SELECTORS`
- `SEARCH_INPUT_SELECTORS`
- `SEARCH_BUTTON_SELECTORS`
- `LIST_ITEM_SELECTORS`
- `DETAIL_PANEL_SELECTORS`
- `DETAIL_CLOSE_SELECTORS`

## 6) 테스트
```bash
python3 -m unittest discover -s tests -v
```

## 7) 유사 정책 검색용 RAG 색인
수집 결과의 `*_part*.json` 색인을 OpenAI Embeddings + Qdrant 검색 색인으로 변환합니다.
RAG 기능을 쓰려면 실행 환경에서 `OPENAI_API_KEY`를 직접 설정하세요.

```bash
python build_policy_rag_index.py prepare-batch \
  --input-glob "2026 전국 정책_*_part*.json" \
  --output-jsonl policy_embedding_batch.jsonl

python build_policy_rag_index.py submit-batch --input-jsonl policy_embedding_batch.jsonl
python build_policy_rag_index.py check-batch <batch_id>
python build_policy_rag_index.py download-batch-output <output_file_id> --output-jsonl policy_embedding_batch_output.jsonl

python build_policy_rag_index.py upsert-batch \
  --input-glob "2026 전국 정책_*_part*.json" \
  --batch-output-jsonl policy_embedding_batch_output.jsonl
```

- `prepare-batch`는 기본적으로 Batch 요청을 파일당 최대 50,000건으로 자동 분할합니다.
- 분할된 경우 `policy_embedding_batch_manifest.json`의 `batch_files`에 나온 각 JSONL을 `submit-batch`로 각각 제출하고, 완료된 출력 JSONL을 각각 `upsert-batch` 하면 됩니다.
- Qdrant 기본 주소: `http://127.0.0.1:6333`
- Qdrant 기본 collection: `policy_cases_v1`
- OpenAI embedding 기본 모델: `text-embedding-3-large`, `dimensions=1536`
- 웹 API: `POST /api/similar-policies` (`query`, `top_k`, `year_from`, `year_to`, `region`, `municipality`, `category`). 웹앱의 CSRF 보호가 적용되므로 브라우저/API 클라이언트에서는 기존 CSRF 토큰을 `X-CSRF-Token` 헤더로 함께 전송합니다.
