# STEP Studio

히스토리가 없는 STEP/IGES 부품에서 **구멍, 필렛, 모따기, 평면(두께)** 을 찾아 바꿀 수 있는 치수로 만들고,
값을 바꿔 가며 변형마다 STEP 파일을 만드는(DOE) 프로그램입니다.

- 파일을 열면 원본 형상과 후보(구멍·필렛·모따기·평면)가 몇 초 안에 보이고, **사용자가 고른 것만** 계산해 치수로 만듭니다.
  같은 크기의 필렛·모따기·구멍은 한 번에 고를 수 있습니다. 면이 많은 부품(기어 등)도 필요한 부분만 빠르게 다룹니다.
- 형상은 그대로 두고 고른 부분만 피처로 분리하므로, 결과는 항상 원본과 같은지 검사됩니다.
- 3D 화면에서 피처를 눌러 위치를 확인하고, 치수를 고치고, 실험 조합을 실행합니다.
- 이 PC 안에서만 동작합니다(주소 `127.0.0.1`). AI 없이도 모든 기능을 쓸 수 있습니다.
- 분석에 쓸 CPU 코어 수를 정할 수 있고(처음에는 절반, 낮은 우선순위), 분석 중에는 단계와 진행률이 표시됩니다.

## 요구사항
| 항목 | 내용 |
|---|---|
| FreeCAD 1.x | 형상 계산 엔진(1.1에서 시험). 프로그램은 FreeCAD에 든 Python으로 돌아가며 **pip 패키지는 필요 없습니다** (`requirements.txt` 참고) |
| Edge 또는 Chrome | 프로그램 화면(WebGL2). Windows에는 Edge가 기본으로 있습니다 |
| Windows 10/11 | 여기서 시험했습니다. Linux/macOS는 `fc.sh`로 실행할 수 있지만 시험하지 않았습니다 |
| Claude Code (선택) | AI가 프로그램을 조작하게 할 때(MCP) |
| AI 모델 서버 (선택) | 창 안의 AI 대화 탭용: Anthropic 방식 게이트웨이, Claude API, Ollama 등. 없어도 모든 기능을 쓸 수 있습니다 |

## 빠른 시작 (Windows)
1. [FreeCAD 1.x](https://www.freecad.org) 설치 (`winget install --id FreeCAD.FreeCAD -e`)
2. 이 저장소를 **Code → Download ZIP**으로 받아 압축 풀기
3. `app.cmd` 더블클릭 → 창에 STEP 파일을 끌어다 놓기

설치부터 사용법까지는 **[MANUAL.md](MANUAL.md)** 에 순서대로 적혀 있습니다.

## AI로 조작하기
AI는 프로그램의 기능 12가지만 호출하고, 형상은 프로그램이 만들고 검사합니다. 두 가지 방법이 있습니다.

- **Claude Code에서 (MCP)**: `claude mcp add steprecon -- cmd /c "C:\steprecon_v2-2\mcp.cmd"`
- **창 안의 AI 대화 탭**: 모델 주소와 토큰을 환경 변수(`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_MODEL`)로 정한 뒤 `app.cmd` 실행. Ollama 등은 `app.cmd --ai openai --base-url ... --model ...`

자세한 내용은 MANUAL.md의 "AI로 조작하기".

## 구성
| 파일 | 역할 |
|---|---|
| `app.cmd / app.py` | STEP Studio 실행 |
| `machined.py, doe.py` | 같은 기능의 명령줄 도구 |
| `step_recon/studio.py, studio.html` | 프로그램의 엔진(로컬 HTTP)과 화면 |
| `step_recon/machined.py` | 구멍·필렛·모따기·평면을 찾아 피처로 분리 |
| `step_recon/cpu.py` | CPU 코어 수와 우선순위 설정 |
| `step_recon/params.py, param_map.py` | 치수 이름과 값, 파라미터 지도 |
| `step_recon/fc_tools.py, constrain.py, compare.py, analyze.py` | FreeCAD 모델 작성, 스케치 구속, 원본 비교, 형상 읽기 |
| `mcp.cmd / mcp_server.py, step_recon/mcp.py` | MCP 서버 |
| `step_recon/tools.py` | AI가 호출하는 기능 12가지 |
| `step_recon/chat.py, llm.py` | AI 대화 탭과 모델 연결 |

테스트: `.\fc.cmd run_tests.py` (15개 파일, 인터넷과 AI 모델 불필요)

## 한계
- 피처가 되는 것은 원통 구멍, 일정 반지름 필렛, 같은 길이 모따기, 평면 이동입니다. 그 밖의 형상은 고정된 소재에 남습니다.
- 실제 주조·가공 부품으로는 아직 충분히 시험하지 못했습니다.
