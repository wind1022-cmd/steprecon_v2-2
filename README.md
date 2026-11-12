# STEP/IGES → FreeCAD 피처 트리 (AI 재구성)

히스토리 없는 STEP/IGES 형상을 LLM이 FreeCAD PartDesign 툴(스케치·패드·포켓·구멍·필렛·회전체·패턴·셸·드래프트 …)로
다시 모델링해서, **수정 가능한 피처 트리(.FCStd)** 로 만듭니다. 원본과의 차이(XOR 부피, 면 개수)를 매번 측정해
틀린 부분을 모델에게 알려 주는 폐루프 구조입니다.

> **처음 쓰는 PC라면 [MANUAL.md](MANUAL.md)부터 보세요.** 내려받기 → 설치 → 로컬 AI 준비 → 내 STEP 폴더 실행 →
> 결과 보기까지 순서대로 적혀 있고, PowerShell 스크립트(.ps1) 실행이 막혔을 때의 방법도 있습니다.

LLM은 교체 가능합니다.

| 백엔드 | 설명 |
|---|---|
| `anthropic` | Claude API (`ANTHROPIC_API_KEY` 필요) |
| `openai` | OpenAI 호환 서버 전부: **Ollama**, LM Studio, vLLM, llama.cpp server … (표준 라이브러리만 사용, 추가 설치 없음) |

## 1. 요구사항
- **FreeCAD 1.x** (pip로 설치되지 않습니다. 별도 설치)
- Claude를 쓸 때만: `anthropic` 패키지 (아래 setup이 `vendor/`에 설치)
- 로컬 모델을 쓸 때만: 실행 중인 OpenAI 호환 서버 (예: Ollama)

## 2. 설치

**Windows (PowerShell)**
```powershell
.\setup.ps1 -InstallFreeCAD     # FreeCAD가 없으면 winget으로 설치 -> 의존성 설치 -> 테스트 실행
.\setup.ps1 -SkipClaude         # 로컬 모델만 쓸 때 (anthropic 패키지 설치 생략)
```
이후 모든 명령은 FreeCAD 번들 Python으로 실행합니다. `fc.ps1`이 자동으로 찾아서 실행해 줍니다.
```powershell
.\fc.ps1 run.py part.step       # = "<FreeCAD>\bin\python.exe" run.py part.step
```
FreeCAD가 특이한 위치에 있으면 `$env:FREECAD_PYTHON = "D:\FreeCAD\bin\python.exe"` 로 지정하세요.

**.ps1 실행이 막힌 PC**: `fc.ps1` 대신 `.\fc.cmd`를 쓰면 됩니다(같은 동작, PowerShell 실행 정책과 무관).
`setup.ps1` 없이도 FreeCAD를 설치하고 `.\fc.cmd run_tests.py`만 통과하면 준비 끝입니다. 자세한 건 MANUAL.md.

**Linux / macOS**
```bash
conda create -n steprecon -c conda-forge freecad python=3.11 && conda activate steprecon
./setup.sh                      # 또는 ./setup.sh --skip-claude
./fc.sh run.py part.step
```
(다른 방식으로 설치했다면 `FREECAD_BIN`=FreeCAD.so가 있는 폴더, `FREECAD_PYTHON`=파이썬 경로)

> Windows 이외 환경은 이 프로젝트를 Windows 11에서만 실제로 실행해 봤습니다. Linux/macOS 경로 탐색 코드는
> 있지만 검증하지 못했습니다.

설치 확인: `.\fc.ps1 run_tests.py` → `22/22 test files passed` (API 키·GPU·데이터셋 불필요, 약 15초)

## 3. 사용법

### Claude API
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
.\fc.ps1 run.py part.step --out out
```
기본 모델은 `claude-opus-5-5`, 툴 24개 전체(`--profile full`)를 사용합니다. 다른 모델: `--model <id>`.

### 로컬 모델 (Ollama)
```powershell
ollama pull qwen3:8b
$env:OLLAMA_CONTEXT_LENGTH = "16384"      # 중요! 아래 참고. 설정 후 Ollama를 재시작해야 적용됨
.\fc.ps1 run.py part.step --backend openai --model qwen3:8b
```
- **컨텍스트 길이**: Ollama의 기본 컨텍스트는 작아서 프롬프트 앞부분(시스템 지침)이 **조용히 잘릴 수 있습니다.**
  실행 시작 때 출력되는 `prompt is about N tokens` 값보다 충분히 크게 설정하세요.
- 다른 서버: LM Studio `--base-url http://localhost:1234/v1`, vLLM `--base-url http://localhost:8000/v1`.
  OpenAI 호환이면 `--api-key`는 환경변수 `STEPRECON_API_KEY`로 줍니다.
- `--profile basic`(로컬 기본값): 툴 9개(pad/pocket/hole/fillet/chamfer/list_edges/compare/undo/finish)와
  짧은 프롬프트. 작은 모델이 헤매지 않게 줄인 구성입니다. 큰 모델이면 `--profile full`.
- `--tool-mode auto`(기본): 네이티브 함수 호출을 먼저 시도하고, 서버가 거부하면 "JSON을 텍스트로 출력" 방식으로
  자동 전환합니다. `native` / `text`로 강제할 수도 있습니다.
- Qwen3·DeepSeek-R1의 `<think>…</think>` 추론 블록은 자동으로 제거됩니다.
- 대화 전체가 `out/<이름>.transcript.json`에 저장되니, 모델이 왜 틀렸는지 확인할 수 있습니다.

### 내 STEP 파일들을 같아질 때까지 반복 시도
```powershell
.\fc.ps1 run.py C:\parts --backend openai --model qwen3:8b --attempts 5 --turns 30 --out out_parts
.\fc.ps1 run.py C:\parts --backend openai --model qwen3:8b --attempts 5 --turns 30 --out out_parts --skip-done   # 이어서
```
- 입력은 파일, **폴더**(하위 폴더의 STEP/IGES 전부), 와일드카드(`parts\*.step`) 모두 됩니다.
- 한 번의 시도 안에서 모델은 `compare`로 원본과의 차이를 보며 최대 `--turns`턴 동안 고칩니다.
- `--attempts N`: 수렴하지 못하면 처음부터 다시 시도합니다(최대 N번). 수렴하면 바로 다음 파일로 넘어갑니다.
  가장 좋은 시도가 `out_parts\`에, 모든 시도가 `out_parts\try1`, `try2` …에 남습니다.
- `--skip-done`: 이미 수렴한 파일은 건너뜁니다. 중간에 끊겼거나 실패한 것만 다시 돌릴 때 씁니다.
- `out_parts\summary.csv`에 파일별 결과(수렴 여부, XOR %, 몇 번째 시도, 걸린 시간)가 파일 하나 끝날 때마다 갱신됩니다.

### 결과물
`out/<이름>.FCStd`(FreeCAD에서 열어 피처 트리 확인) + `out/<이름>.recon.json`(수렴 여부, XOR %, 피처 목록, 턴 수).
수렴 기준: XOR < 0.1% 이고 면 개수 일치.

### 다중 바디 (서로 떨어진 솔리드)
파일에 솔리드가 여러 개면, 겹치거나 맞닿은 것은 로드할 때 하나로 병합하고 떨어져 있는 것은 그대로 둡니다.
요약 JSON의 `n_solids`가 2 이상이면 `solids` 목록(솔리드별 부피·경계 상자·면 번호)이 붙고 각 면에 `solid` 번호가 달립니다.
에이전트는 솔리드 하나당 PartDesign Body 하나를 만듭니다.
- `new_body`: 새 바디를 만들고 활성 바디로 전환 (현재 바디에 형상이 있어야 함)
- `select_body`: 이전 바디로 돌아가기 (0부터 시작하는 번호)
- 모든 툴(엣지/면 번호, 포켓, 필렛, 패턴)은 **활성 바디에만** 작용합니다. 패턴/미러는 바디를 넘지 못합니다.
- `compare`는 항상 **모든 바디를 합쳐서** 원본 전체와 비교합니다. 바디끼리 겹치면 합쳐서 비교하고 힌트로 알려 줍니다.
- `undo`는 마지막 단계를 그 단계가 속한 바디에서 지우고, 빈 바디가 되면 바디 자체를 지웁니다.
- 이 툴들은 `--profile full`에만 있습니다(`basic`은 단일 바디 전용).

### 구멍 · 스윕 · 로프트
- `hole`은 FreeCAD의 네이티브 Hole 피처입니다. 한 번의 호출로 카운터보어(`counterbore_diameter`/`counterbore_depth`),
  카운터싱크(`countersink_diameter`/`countersink_angle`), 드릴 끝 원뿔(`drill_angle`, 깊이는 원통 부분 기준),
  같은 크기의 구멍 여러 개(`positions`)를 만듭니다. 옵션 없는 단순 구멍이 실패하면 원형 포켓으로 대체합니다.
- `sweep`: 닫힌 단면을 직선+원호로 된 **평면 경로**를 따라 쓸어 만듭니다(파이프, 손잡이, 굽은 봉). `cut=true`면 홈.
  단면 스케치는 경로의 끝점에, 그 지점의 경로 방향과 수직인 평면에 있어야 합니다.
- `loft`: 서로 다른 평면의 닫힌 단면 2개 이상을 순서대로 잇습니다. `ruled=true`면 직선으로 이어서
  원→원은 원뿔, 다각형→다각형은 평평한 옆면이 됩니다. `cut=true`면 제거.
- FreeCAD PartDesign에는 **네이티브 Rib 피처가 없습니다.** `rib`는 그대로 직선 웹(패드)이고, 삼각 보강 리브는
  `pad`에 삼각형 단면과 `midplane=true`를 쓰면 됩니다.

### 데이터셋 없이 빠르게 시험하기
```powershell
.\fc.ps1 make_synth.py --n 10                              # data\synth\synth_000.step ... (정답 히스토리 .txt 포함)
.\fc.ps1 run.py data\synth\synth_003.step --backend openai --model qwen3:8b --out out_local
.\fc.ps1 index_dataset.py data\synth --out data\synth_idx.csv
.\fc.ps1 eval_batch.py data\synth_idx.csv --n 10 --backend openai --model qwen3:8b --out out_local
```
같은 시드는 어느 PC에서나 같은 부품을 만듭니다. 블록 + 포켓/구멍/필렛 조합이라 `basic` 프로파일로 풀 수 있습니다.

## 4. 데이터셋으로 일괄 평가 (선택)
Fusion 360 Gallery 재구성 데이터셋 사용. **라이선스상 비상업적 연구 목적 전용**이며 재배포 금지입니다.
```powershell
python download_fusion.py --yes                       # 약 2GB, 일반 Python으로 실행 가능
.\fc.ps1 fusion_index.py data\fusion360\r1.0.1\reconstruction --out data\fusion_idx.csv   # 8,625개 분류 (약 40초)
.\fc.ps1 eval_batch.py data\fusion_idx.csv --cat prismatic --max-faces 30 --n 20 --dry   # 선택 목록만 확인
.\fc.ps1 eval_batch.py data\fusion_idx.csv --cat prismatic --max-faces 30 --n 20 --out out_eval
.\fc.ps1 eval_batch.py data\fusion_idx.csv --n 20 --backend openai --model qwen3:8b --out out_local
```
`out_eval/results.csv`에 파일별 결과(백엔드·모델 포함)가 쌓이고, 범주별 성공률이 출력됩니다.
기본값은 단일 바디 설계만 고릅니다(`--max-solids 1`). 다중 바디 설계도 포함하려면 `--max-solids 4`처럼 올리세요.
결과에 `n_solids` 열이 있고, 다중 바디가 섞이면 단일/다중 성공률이 따로 출력됩니다.
같은 `--seed`면 같은 파일이 선택되므로 Claude와 로컬 모델을 같은 문제로 비교할 수 있습니다.
임의의 STEP 폴더는 `index_dataset.py <폴더> --out idx.csv`로 같은 방식으로 분류할 수 있습니다.

## 5. 구성
```
step_recon/
  analyze.py   STEP/IGES 로드(다중 솔리드 병합, 뒤집힌 방향 보정) + 면/엣지 요약 JSON + 곡면 클러스터 + 솔리드별 표
  compare.py   원본 대비 XOR/부피/면·솔리드 개수 (곡면이 있거나 불리언 결과가 부피와 모순되면 점 샘플링으로 교차 검증)
  fc_tools.py  FreeCAD PartDesign 툴: pad pocket hole fillet chamfer revolution linear/polar_pattern mirror
               sweep loft shell draft rib import_freeform new_body select_body ... undo save
  agent.py     LLM tool-use 루프, 시스템 프롬프트, 툴 정의, profile(full/basic)
  llm.py       백엔드: AnthropicBackend, OpenAICompatBackend(+JSON 텍스트 폴백)
  cli.py       run.py / eval_batch.py 공통 옵션
  dataset.py, fusion.py   분류기(prismatic/revolved/freeform/mixed)와 Fusion 어댑터
  synth.py     정답이 있는 합성 부품 생성
run.py  eval_batch.py  make_synth.py  fusion_index.py  index_dataset.py  download_fusion.py  run_tests.py
fc.ps1 / fc.cmd / fc.sh   FreeCAD 번들 Python으로 스크립트 실행      MANUAL.md   처음부터 따라 하는 설명서
tests/   dev/
```

## 6. 현재 상태와 한계 (솔직하게)
- 툴과 비교 로직은 **합성 형상·실제 Fusion 설계 몇 개에서 XOR 0.0%로 검증**했습니다. 그러나 이 검증들은 사람이
  툴을 직접 호출한 것이므로, **LLM 에이전트의 실제 성공률은 측정되지 않았습니다.** 이 PC가 아닌 환경에서
  `eval_batch.py`로 측정하는 것이 다음 단계입니다.
- 자유곡면(B-spline)은 피처로 재구성하지 못합니다. `import_freeform`이 곡면 부분만 원본 그대로(편집 불가)
  가져오고 나머지만 피처로 만듭니다.
- `rib`는 직선 하나짜리 얇은 벽(패드)입니다(FreeCAD에 네이티브 Rib가 없음). `hole`은 나사산을 만들지 않습니다.
- 스윕 경로는 한 평면 위의 직선·원호만 됩니다(3차원 경로, 스플라인, 나선 불가). 로프트는 가이드 곡선이 없습니다.
- 구멍 옵션·스윕·로프트는 합성 형상으로만 검증했습니다(`tests/test_stage4.py`). Fusion 데이터셋은 스케치+돌출만
  담고 있어서 이 피처들의 정답이 없습니다. LLM이 언제 이 툴을 고르는지도 측정하지 않았습니다.
- 3개 이상의 단면을 매끄럽게(`ruled=false`) 이은 로프트는 B-spline 면이 되어, 수렴 판정이 샘플링 근사로 넘어갑니다.
- 다중 바디는 스크립트로 만든 패드로만 검증했습니다(합성 형상 + Fusion 설계 74개, `dev/multibody_fusion_check.py`).
  LLM이 `new_body`를 제대로 쓰는지는 측정하지 않았습니다. `import_freeform`은 여전히 첫 단계에서만 쓸 수 있어서
  자유곡면이 있는 다중 바디는 고정 영역이 한 바디에 몰립니다.
- 곡면 포함 형상의 수렴 판정은 샘플링 근사입니다. 해석 형상에서도 OCC 불리언이 부피와 모순되는 답을 내면
  샘플링으로 대체하며(결과의 `xor_method: sampled`), 이때는 부피 차이 0.1% 미만도 함께 요구합니다.
- 작은 로컬 모델(8B급)은 20개 툴과 긴 지침을 안정적으로 쓰기 어려울 가능성이 큽니다. `basic` 프로파일로 시작하고,
  가능하면 더 큰 모델을 쓰세요. `transcript.json`으로 실패 원인을 확인할 수 있습니다.

## 7. 문제 해결
| 증상 | 조치 |
|---|---|
| `FreeCAD was not found` | FreeCAD 설치 확인, `FREECAD_BIN`(FreeCAD.pyd 폴더) 또는 `FREECAD_PYTHON` 지정 |
| `.ps1` 실행이 차단됨 | `.\fc.cmd <스크립트> ...` 사용, 또는 `powershell -ExecutionPolicy Bypass -File .\fc.ps1 ...` (MANUAL.md) |
| `cannot reach LLM server` | Ollama 실행 여부(`ollama list`), `--base-url` 확인 |
| 로컬 모델이 도구를 안 부름 | `--tool-mode text`, `--profile basic`, transcript 확인 |
| 모델이 지침을 무시하는 듯함 | 컨텍스트 길이 부족 → `OLLAMA_CONTEXT_LENGTH` 상향 후 Ollama 재시작 |
| `ANTHROPIC_API_KEY is not set` | 키 설정 또는 `--backend openai` |

## 8. 라이선스/출처
Fusion 360 Gallery Dataset: Autodesk, 비상업적 연구 전용 (아카이브 내 라이선스 문서 참조). 이 저장소에는 데이터를
포함하지 않으며(`.gitignore`), 포함하지 마세요.
