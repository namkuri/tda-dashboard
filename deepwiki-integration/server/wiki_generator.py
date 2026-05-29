"""[r113→r122] DeepWiki-Open 스타일 자동 위키 페이지 생성기.

OSS 참고: github.com/AsyncFuncAI/deepwiki-open
출력 형식 참고: deepwiki.com

핵심 알고리즘:
  1) Git clone (기본 브랜치 자동 감지, r117)
  2) 파일 트리 분석 → 시스템 단위 카테고리 자동 분류
  3) 각 카테고리에 대해 LLM 호출:
     - System Architecture (Mermaid 다이어그램)
     - Core Components (클래스/함수 표 + Sources 인용)
     - Data Flow (Mermaid 시퀀스)
     - Key Features
     - Sources (file:line 자동 추출)
  4) 전체 Architecture 페이지 1개 (LLM 호출, 시스템 간 관계 Mermaid)
  5) Overview 페이지 (자동 생성, 카테고리 인덱스 + Mermaid)
  6) deep_wiki_pages 테이블에 upsert (repo_name + generation_id 버전 관리)
"""
import os
import re
import json
import shutil
import stat
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Dict, Any, List, Optional, Tuple
import git

from config import settings
from ollama_client import get_ollama
from supabase_store import get_store
from indexer import _safe_remove_repo, clone_with_fallback, CODE_EXTENSIONS, SKIP_DIRS


# ─────────────────────────────────────────────
# 카테고리 분류 — 시스템 단위 세분화 (r122)
# DeepWiki 스타일: "Core Player Systems / Inventory and Crafting" 같이 시스템 단위
# ─────────────────────────────────────────────

# (정규식, 표시 제목, 슬러그, 정렬 우선순위)
# 더 구체적인 패턴이 더 일반적인 것보다 먼저 와야 함
_SYSTEM_PATTERNS: List[Tuple[str, str, str, int]] = [
    # 유니티 — 세분화된 시스템
    (r"Assets/Scripts/Managers?/Player\b", "Player Manager", "player-manager", 10),
    (r"Assets/Scripts/Managers?/Stats?\b", "Stats Management", "stats-management", 11),
    (r"Assets/Scripts/Managers?/Game\b", "Game Manager", "game-manager", 12),
    (r"Assets/Scripts/Managers?/Inventory\b", "Inventory Manager", "inventory-manager", 13),
    (r"Assets/Scripts/Managers?\b", "Managers (Core)", "managers", 14),
    (r"Assets/Scripts/Player/Movement\b|Assets/Scripts/Movement\b", "Movement & Locomotion", "movement", 20),
    (r"Assets/Scripts/Player/Combat\b|Assets/Scripts/Combat\b", "Combat System", "combat", 21),
    (r"Assets/Scripts/Player/Equipment\b|Assets/Scripts/Equipment\b", "Equipment & Weapons", "equipment", 22),
    (r"Assets/Scripts/Player\b", "Player (General)", "player", 23),
    (r"Assets/Scripts/Inventory\b", "Inventory System", "inventory", 30),
    (r"Assets/Scripts/Crafting\b", "Crafting System", "crafting", 31),
    (r"Assets/Scripts/Items?\b", "Item Data & Definitions", "items", 32),
    (r"Assets/Scripts/UI/Inventory\b", "Inventory UI", "inventory-ui", 33),
    (r"Assets/Scripts/UI\b", "UI System", "ui", 35),
    (r"Assets/Scripts/Character\b", "Character Manager Base", "character", 40),
    (r"Assets/Scripts/Effects?\b|Assets/Scripts/VFX\b", "Effects System", "effects", 41),
    (r"Assets/Scripts/Damage\b", "Damage System", "damage", 42),
    (r"Assets/Scripts/Enemy\b|Assets/Scripts/Enemies\b|Assets/Scripts/AI\b", "Enemies & AI", "enemies", 43),
    (r"Assets/Scripts/Quest\b|Assets/Scripts/Quests\b", "Quest System", "quests", 50),
    (r"Assets/Scripts/Dialogue\b", "Dialogue System", "dialogue", 51),
    (r"Assets/Scripts/Save\b|Assets/Scripts/Persistence\b", "Save System", "save", 52),
    (r"Assets/Scripts/Audio\b|Assets/Scripts/Sound\b", "Audio System", "audio", 53),
    (r"Assets/Scripts/Scene\b|Assets/Scripts/World\b", "Scene & World", "scene-world", 60),
    (r"Assets/Scripts/Network\b|Assets/Scripts/Net\b|Mirror\b|Photon\b|NetCode\b", "Network (Mirror/Photon/Netcode)", "network", 61),
    # [r132] Unity 추가 세분화 — 자주 보이는 시스템
    (r"Assets/Scripts/Input\b|InputSystem\b", "Input System", "input-system", 62),
    (r"Assets/Scripts/Animation\b|Animations?/", "Animation & Animator", "animation", 63),
    (r"Cinemachine\b|Assets/Scripts/Camera\b", "Camera & Cinemachine", "camera", 64),
    (r"Localization\b|Assets/Localizations?\b", "Localization", "localization", 65),
    (r"Assets/Tests?\b|PlayMode/|EditMode/", "Tests (PlayMode/EditMode)", "tests-unity", 66),
    (r"Shaders?/|\.shader$|\.cginc$", "Shaders & Materials", "shaders", 67),
    (r"VFX\b|VisualEffects?\b|\.vfx$", "VFX Graph", "vfx-graph", 68),
    (r"Assets/Editor\b|Editor/", "Editor Extensions", "editor", 70),
    (r"Assets/Scripts/Utils?\b|Assets/Scripts/Helpers?\b", "Utility & Helpers", "utils", 71),
    (r"Assets/Scripts\b", "Scripts (Misc)", "scripts-misc", 80),
    (r"Assets/Resources\b", "Resources", "resources", 81),
    (r"Assets/Prefabs?\b", "Prefabs", "prefabs", 82),
    (r"Assets/ScriptableObjects?\b|Assets/SO\b", "Scriptable Objects", "so", 83),
    # 일반 웹/앱
    (r"^src/components?\b", "Components", "components", 100),
    (r"^src/pages?\b|^pages?\b", "Pages", "pages", 101),
    (r"^src/api\b|^api\b|^server\b", "API / Server", "api", 102),
    (r"^src/services?\b", "Services", "services", 103),
    (r"^src/utils?\b|^lib\b|^utils?\b", "Utilities", "utils-web", 104),
    (r"^src/models?\b|^models?\b", "Data Models", "models", 105),
    (r"^src/store\b|^src/state\b|^store\b", "State Management", "store", 106),
    (r"^src/hooks?\b|^hooks?\b", "Hooks", "hooks", 107),
    (r"^src\b", "Source (src)", "src-main", 110),
    # 문서/설정
    (r"^docs?\b", "Documentation", "docs", 120),
    (r"^migrations?\b", "Database Migrations", "migrations", 121),
    (r"^tests?\b|__tests__|spec/", "Tests", "tests", 122),
    (r"^scripts?\b", "Build Scripts", "scripts", 123),
    (r"\.github\b|^github\b", "GitHub Workflows", "github", 124),
]


def _classify_file(rel_path: str) -> Optional[Tuple[str, str, int]]:
    """파일 경로 → (title, slug, priority). 매치 없으면 None → root 카테고리."""
    norm = rel_path.replace("\\", "/")
    for pat, title, slug, prio in _SYSTEM_PATTERNS:
        if re.search(pat, norm, re.IGNORECASE):
            return title, slug, prio
    return None


def _scan_repo(repo_path: Path, max_files_per_category: int = 15) -> Dict[str, Dict[str, Any]]:
    """[r122] 파일 트리 → 시스템 카테고리 분류."""
    categories: Dict[str, Dict[str, Any]] = {}
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if any(skip in path.parts for skip in SKIP_DIRS):
            continue
        if path.suffix.lower() not in CODE_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
            if size > settings.MAX_FILE_SIZE_KB * 1024:
                continue
        except Exception:
            continue
        rel = str(path.relative_to(repo_path)).replace("\\", "/")
        cls = _classify_file(rel)
        if not cls:
            cls = ("Project Configuration", "project-config", 999)
        title, slug, prio = cls
        if slug not in categories:
            categories[slug] = {
                "title": title,
                "slug": slug,
                "priority": prio,
                "files": [],
                "total_files": 0,
            }
        categories[slug]["total_files"] += 1
        if len(categories[slug]["files"]) < max_files_per_category:
            try:
                line_count = 0
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for _ in f:
                        line_count += 1
            except Exception:
                line_count = 0
            categories[slug]["files"].append({
                "path": rel,
                "abs": str(path),
                "size": size,
                "ext": path.suffix.lower(),
                "lines": line_count,
            })
    return categories


def _read_file_truncated(abs_path: str, max_chars: int = 4500) -> str:
    """파일 본문 읽기. 너무 크면 잘라냄."""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read(max_chars + 100)
        if len(data) > max_chars:
            return data[:max_chars] + "\n\n// ... (truncated)"
        return data
    except Exception as e:
        return f"(read failed: {e})"


# ─────────────────────────────────────────────
# LLM 프롬프트 — DeepWiki 형식 (Sources 인용 + Mermaid + 표)
# ─────────────────────────────────────────────

# [r132] Unity 프로젝트 감지 — 다음 단서가 있으면 Unity 모드
_UNITY_MARKERS = (
    "/Assets/",          # 거의 모든 Unity 프로젝트 (대소문자 무관 매치)
    "/ProjectSettings/",
    "/Packages/manifest.json",
)
_UNITY_EXTENSIONS = (".unity", ".prefab", ".asset", ".meta", ".asmdef", ".asmref", ".mat", ".controller", ".anim", ".shader", ".cginc")


def _detect_unity_project(repo_path: Path) -> bool:
    """[r132] 레포가 Unity 프로젝트인지 — 디렉토리·확장자 단서로 판정."""
    try:
        # 1) 디렉토리 단서 (가장 강함)
        for marker in ("Assets", "ProjectSettings"):
            if (repo_path / marker).is_dir():
                return True
        # 2) Unity 전용 확장자 파일 존재
        for sub in repo_path.rglob("*"):
            if sub.is_file() and sub.suffix.lower() in _UNITY_EXTENSIONS:
                return True
            # 너무 깊이 들어가지 않게 — 100개 파일 검사 후 단념
        return False
    except Exception:
        return False


# [r132] Unity 모드에서 system message 에 덧붙일 한국어 프롬프트
_UNITY_PROMPT_ADDENDUM = """

## 🎮 Unity 프로젝트 특화 분석 (Unity Mode ON)

이 코드는 **Unity 엔진 프로젝트**입니다. 일반 C# 분석이 아니라 Unity 특화 패턴까지 캐치하세요:

### 반드시 캐치할 Unity 패턴
1. **MonoBehaviour 라이프사이클** — 사용된 콜백을 명시:
   - 초기화: `Awake`, `OnEnable`, `Start`
   - 매 프레임: `Update`, `FixedUpdate`(물리), `LateUpdate`(카메라)
   - 종료: `OnDisable`, `OnDestroy`
   - 충돌: `OnTriggerEnter/Stay/Exit`, `OnCollisionEnter/Stay/Exit` (2D 버전 포함)
   - 에디터: `OnValidate`, `OnDrawGizmos`
2. **ScriptableObject 패턴**:
   - `[CreateAssetMenu(menuName="...", fileName="...")]` → 에셋 생성 가능 여부
   - SO 인스턴스가 다른 컴포넌트에서 참조되는 방식 (data-as-asset 패턴)
3. **Inspector 노출 필드**:
   - `[SerializeField]` private 필드 + public 필드
   - 어트리뷰트: `[Header("...")]`, `[Range(min,max)]`, `[Tooltip]`, `[Min]`, `[Max]`, `[ColorUsage]`, `[TextArea]`
   - 숨김: `[HideInInspector]`, `[NonSerialized]`
4. **컴포넌트 의존성**:
   - `[RequireComponent(typeof(Rigidbody))]`, `[DisallowMultipleComponent]`, `[ExecuteInEditMode]`
   - `GetComponent<T>()`, `GetComponentInChildren<T>()`, `GetComponentInParent<T>()` 호출 흐름
5. **코루틴**:
   - `IEnumerator` 반환 메서드 + `StartCoroutine`/`StopCoroutine`/`StopAllCoroutines`
   - `yield return new WaitForSeconds(n)`, `WaitUntil(...)`, `WaitForEndOfFrame`
   - **비동기 흐름 mermaid sequenceDiagram 으로 표현**
6. **에디터 확장**:
   - `Editor`, `EditorWindow`, `PropertyDrawer` 상속
   - `[CustomEditor(typeof(...))]`, `[CustomPropertyDrawer(typeof(...))]`
   - `Editor/` 폴더에 있는 스크립트는 빌드 제외 — 별도 섹션
7. **에셋·리소스 시스템**:
   - `Resources.Load` (안티패턴 경고), `AssetBundle` (레거시)
   - `Addressables`, `AssetReference` (권장 패턴)
8. **Input**:
   - 신 Input System (`PlayerInput`, `InputAction`, `InputActionAsset`) vs 레거시 `Input.GetKey`
9. **물리**:
   - `Rigidbody`/`Rigidbody2D`, `Collider`/`Collider2D`, layers, `Physics.Raycast`
   - **`FixedUpdate`에서만 물리 업데이트** — 다른 곳이면 경고
10. **UI**:
    - `Canvas` 렌더 모드 (Overlay/Camera/WorldSpace), `RectTransform` 앵커
    - `TextMeshPro` vs Legacy `Text`
    - `EventTrigger`, `IPointerClickHandler`, `Button.onClick` 패턴
11. **에셈블리 정의**: `*.asmdef` 파일이 보이면 어셈블리 분리 구조 명시
12. **씬·프리팹**: `*.unity`(씬), `*.prefab` 파일 존재 시 명시

### 출력 시 추가 섹션 (해당 패턴 발견 시 반드시 추가)

기존 7섹션 외에 다음을 **Core Components 다음에** 끼워넣기:

#### 🎮 Unity 컴포넌트 메타 (해당 시)

**MonoBehaviour 라이프사이클 사용 현황**:
| 클래스 | 사용한 콜백 | 용도 |
|--------|-----------|------|
| `PlayerController` | `Awake`, `Update`, `FixedUpdate` | 초기화 + 입력 + 물리 |

**Inspector 노출 필드 (`[SerializeField]` + public)**:
| 클래스 | 필드 | 타입 | 어트리뷰트 | 용도 |
|--------|------|------|----------|------|
| `PlayerController` | `moveSpeed` | `float` | `[Range(0, 20)]` | 이동 속도 |

**컴포넌트 의존성**:
- `PlayerController` 는 `[RequireComponent(typeof(Rigidbody))]` → 자동으로 `Rigidbody` 부착됨

**코루틴 흐름** (있으면 Mermaid):
```mermaid
sequenceDiagram
    참여자 명시
    yield 시점 명시
```

**ScriptableObject 자산** (해당 시):
- `PlayerStatsData` — `[CreateAssetMenu]` 로 `Assets/Data/Stats/...` 에 인스턴스 생성

각 항목 발견 시 `[path/file.cs:N-M]` Sources 인용 필수.

### 일반 C# 과의 차이
- `void Update()` 같은 빈 시그니처라도 Unity 엔진이 매 프레임 호출 → "엔트리포인트"로 간주
- `public` 필드 = 자동으로 Inspector 노출 (private + `[SerializeField]` 와 동등)
- `static` 매니저 패턴 (e.g. `PlayerManager.Instance`) 가 흔함 — DI 가 아니라 글로벌 싱글톤 명시
"""


_CATEGORY_PROMPT = """당신은 DeepWiki(deepwiki.com) 스타일의 시니어 코드 위키 작성자입니다. 아래 코드 파일들을 정독하고 시스템에 대한 종합 분석 페이지를 작성하세요.

# 🚨 절대 규칙
1. **한국어로 답변**. 영문 답변 금지. 식별자·파일경로·코드만 원문 유지.
2. **Sources 인용 형식 정확히 준수**: 모든 클래스·메서드·필드 언급 시 반드시 출처 표시 → `[path/file.cs:N-M]` 형태로 인라인 추가. 예: "`PlayerManager` 클래스 [Assets/Scripts/Managers/Player/PlayerManager.cs:5-44]"
3. 코드에 없는 사실 만들지 말 것. 추측은 "~로 보입니다" 명시.
4. 표·Mermaid·코드블록 적극 사용.

# 📋 출력 형식 — 다음 7개 섹션을 정확한 헤더로

# {{시스템 제목}}

## Overview
이 시스템의 책임과 주요 사용처를 4~6문장. 어떤 문제를 해결하고 어디서 호출되는지. 핵심 클래스·매니저 1~3개를 굵게 강조하면서 첫 등장 시 Sources 추가:
> 예: **PlayerManager** [Assets/Scripts/Managers/Player/PlayerManager.cs:5-44] 는 ...

## System Architecture
시스템의 계층·구조·디자인 패턴을 설명. 반드시 Mermaid 다이어그램 1개 이상:

```mermaid
graph TD
    A["엔트리포인트<br/>(PlayerController)"] --> B["핵심<br/>(PlayerManager)"]
    B --> C["서브시스템<br/>(StatsManager)"]
```

다이어그램 후 각 노드가 무엇인지 1~2줄씩 설명.

## Core Components
주요 클래스·메서드·인터페이스를 표로 정리. 모든 행에 Sources 포함:

| 컴포넌트 | 파일 (Sources) | 종류 | 역할 |
|----------|----------------|------|------|
| `ClassName` | `path/file.cs:5-44` | 클래스 | 한 줄 설명 |
| `MethodName` | `path/file.cs:80-110` | 메서드 | 한 줄 설명 |

## Data Flow
주요 호출 흐름·이벤트 흐름·상태 전환을 Mermaid sequence 또는 stateDiagram으로:

```mermaid
sequenceDiagram
    participant U as User
    participant P as PlayerManager
    participant S as StatsManager
    U->>P: Input
    P->>S: UpdateStats()
    S-->>P: NewStats
```

그 아래 흐름 1~2단락 한국어 설명.

## Key Features
이 시스템이 제공하는 주요 기능을 불릿으로 5~10개. 각 기능에 핵심 코드 위치 Sources 추가:
- **체력 회복 시스템**: 자연 회복·아이템·스킬 3종 [Assets/Scripts/Managers/Stats/StatsManager.cs:120-180]
- ...

## Code References
핵심 코드 1~3개를 발췌. 각 발췌 위에 파일경로+라인 명시, 발췌 아래 짧은 설명:

**`Assets/Scripts/Managers/Player/PlayerManager.cs:5-44`**
```csharp
public class PlayerManager : MonoBehaviour {
    public static PlayerManager Instance;
    // ...
}
```
싱글톤 패턴으로 글로벌 접근 제공.

## Sources
이 페이지가 참조한 모든 파일을 글머리 기호로:
- `Assets/Scripts/Managers/Player/PlayerManager.cs`
- `Assets/Scripts/Managers/Stats/StatsManager.cs`
- ...

---

# 📦 입력 데이터
- **시스템**: {category_title}
- **슬러그**: `{slug}`
- **총 파일**: {total_files}개 (아래는 본문이 첨부된 {shown_files}개)

{files_block}
"""


_ARCHITECTURE_PROMPT = """당신은 DeepWiki(deepwiki.com) 스타일의 시니어 시스템 아키텍트입니다. 아래 카테고리 목록과 각 카테고리의 핵심 파일들을 보고 **프로젝트 전체 아키텍처 페이지**를 한 장 작성하세요.

# 🚨 절대 규칙
1. **한국어**. 식별자·파일경로는 원문 유지.
2. Sources 인용 형식: `[path/file.cs]` 또는 `[path/file.cs:N-M]`
3. 추측 금지 — 코드에 명시된 사실만.

# 📋 출력 형식

# {{프로젝트 이름}} — System Architecture

## Overall Architecture Diagram
프로젝트의 모든 서브시스템을 큰 Mermaid 다이어그램 1개로 표현. 카테고리 = 노드, 호출/의존 관계 = 화살표.

```mermaid
graph TB
    subgraph "Input Layer"
        IH[InputHandler]
    end
    subgraph "Player Systems"
        PM[PlayerManager]
        PSM[PlayerStatsManager]
    end
    subgraph "Combat"
        CS[CombatSystem]
        DS[DamageSystem]
    end
    IH --> PM
    PM --> PSM
    PM --> CS
    CS --> DS
```

## Core Components
프로젝트의 메인 컴포넌트들을 표로:

| Layer | Component | 파일 | 역할 |
|-------|-----------|------|------|
| Input | `InputHandler` | `Assets/Scripts/Utilities/InputHandler.cs` | 키 입력 처리 |
| Player | `PlayerManager` | `Assets/Scripts/Managers/Player/PlayerManager.cs` | 플레이어 중심 허브 |

## Data Flow Examples
프로젝트에서 일어나는 대표 흐름 2~3개를 Mermaid sequenceDiagram으로:

### Input Processing Flow
```mermaid
sequenceDiagram
    participant U as User
    participant I as InputHandler
    participant P as PlayerManager
    U->>I: KeyPress
    I->>P: OnAction
```

### Inventory Item Flow (예시 — 실제 코드에 있다면)
```mermaid
sequenceDiagram
    ...
```

## Key Features
프로젝트 전체의 핵심 기능 5~8개:
- **체력/스태미나 시스템**: ...
- **인벤토리 그리드**: ...
- **콤보 공격**: ...

## Subsystems
각 서브시스템(카테고리)에 한 줄 요약 + Deep Wiki 페이지 링크:

- 📘 **[Player Manager](?slug=player-manager)** — 플레이어 중심 허브
- 📘 **[Stats Management](?slug=stats-management)** — 체력·스태미나·경험치
- 📘 **[Combat System](?slug=combat)** — 공격·피격·콤보
- ...

---

# 📦 입력 데이터
- **프로젝트 Repo**: {git_url}
- **분석 커밋**: `{commit}`
- **카테고리 수**: {category_count}

{categories_block}
"""


def _build_files_block(files: List[Dict[str, Any]]) -> str:
    """LLM에 첨부할 파일 본문 블록 + 라인 정보."""
    blocks = []
    for f in files:
        text = _read_file_truncated(f["abs"], max_chars=4500)
        lines = f.get("lines", 0)
        blocks.append(f"### `{f['path']}` ({lines}줄, {f['size']} bytes)\n```\n{text}\n```")
    return "\n\n---\n\n".join(blocks)


def _build_categories_block(categories: List[Dict[str, Any]], top_files: int = 3) -> str:
    """LLM Architecture 페이지 입력 — 카테고리별 요약 + 핵심 파일 시그니처."""
    blocks = []
    for cat in categories:
        files_preview = []
        for f in cat["files"][:top_files]:
            sig = _read_file_truncated(f["abs"], max_chars=800)  # 시그니처 정도만
            files_preview.append(f"- `{f['path']}` ({f['lines']}줄)")
        blocks.append(
            f"### {cat['title']} (`{cat['slug']}`) — {cat['total_files']}개 파일\n"
            + "\n".join(files_preview)
        )
    return "\n\n".join(blocks)


async def _llm_generate_category_page(
    cat: Dict[str, Any],
    model: Optional[str] = None,
    unity_mode: bool = False,
) -> Dict[str, Any]:
    """카테고리 페이지 생성 — DeepWiki 7섹션 형식. unity_mode=True 면 Unity 특화 addendum."""
    prompt = _CATEGORY_PROMPT.format(
        category_title=cat["title"],
        slug=cat["slug"],
        total_files=cat["total_files"],
        shown_files=len(cat["files"]),
        files_block=_build_files_block(cat["files"]),
    )
    ollama = get_ollama()
    system_msg = (
        "당신은 시니어 소프트웨어 아키텍트 + 테크니컬 라이터입니다. "
        "DeepWiki(deepwiki.com)와 동등한 수준의 코드 위키를 작성합니다.\n"
        "🚨 규칙:\n"
        "1. 반드시 한국어. 영문 응답 금지. 식별자·경로·코드만 원문 유지.\n"
        "2. 사용자가 지정한 7섹션(Overview / System Architecture / Core Components / Data Flow / Key Features / Code References / Sources) 정확히 따를 것.\n"
        "3. 모든 클래스·메서드 언급 시 [path/file.cs:N-M] 형태 Sources 인용 추가.\n"
        "4. Mermaid 다이어그램(graph/sequenceDiagram/stateDiagram) 최소 2개 포함.\n"
        "5. 코드에 명시되지 않은 정보 만들지 말 것."
    )
    # [r132] Unity 프로젝트 감지되면 system 에 특화 분석 지시 추가
    if unity_mode:
        system_msg += _UNITY_PROMPT_ADDENDUM
    content_chunks: List[str] = []
    async for d in ollama.chat_stream(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
        model=model,
        temperature=0.25,
    ):
        content_chunks.append(d)
    content = "".join(content_chunks).strip()
    title = cat["title"]
    first_line = content.split("\n", 1)[0].strip()
    if first_line.startswith("#"):
        cleaned = first_line.lstrip("# ").strip()
        if cleaned:
            title = cleaned
    summary = _extract_summary(content)
    sources = _extract_sources(content, cat["files"])
    return {"title": title, "content": content, "summary": summary, "sources": sources}


async def _llm_generate_architecture_page(
    categories: List[Dict[str, Any]],
    git_url: str,
    commit: str,
    repo_name: str,
    model: Optional[str] = None,
    unity_mode: bool = False,
) -> Dict[str, Any]:
    """전체 Architecture 페이지 — 모든 카테고리 관계 Mermaid."""
    prompt = _ARCHITECTURE_PROMPT.format(
        git_url=git_url,
        commit=commit or "?",
        category_count=len(categories),
        categories_block=_build_categories_block(categories, top_files=3),
    )
    ollama = get_ollama()
    system_msg = (
        "당신은 시니어 시스템 아키텍트입니다. DeepWiki(deepwiki.com)와 동등 수준의 "
        "프로젝트 전체 아키텍처 분석을 작성합니다.\n"
        "🚨 규칙:\n"
        "1. 한국어. 영문 답변 금지.\n"
        "2. Sources 인용: `[path/file.cs]` 형식.\n"
        "3. Overall Architecture Diagram은 반드시 큰 Mermaid graph TB 로 작성 (subgraph 활용).\n"
        "4. Data Flow Examples는 sequenceDiagram 2~3개.\n"
        "5. 추측 금지 — 코드 외 사실 작성 금지."
    )
    if unity_mode:
        system_msg += _UNITY_PROMPT_ADDENDUM
    content_chunks: List[str] = []
    async for d in ollama.chat_stream(
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
        model=model,
        temperature=0.2,
    ):
        content_chunks.append(d)
    content = "".join(content_chunks).strip()
    title = f"{repo_name} — System Architecture"
    summary = _extract_summary(content)
    return {"title": title, "content": content, "summary": summary, "sources": []}


def _extract_summary(md: str) -> str:
    """첫 일반 단락 추출 (Overview 단락)."""
    m = re.search(r"##\s*Overview\s*\n+([^\n#]+(?:\n[^\n#][^\n]*)*)", md)
    if m:
        text = m.group(1).strip()
    else:
        parts = [p.strip() for p in md.split("\n\n")]
        text = next((p for p in parts if p and not p.startswith("#") and not p.startswith("```")), "")
    text = re.sub(r"\s+", " ", text)
    return text[:250] + ("…" if len(text) > 250 else "")


_SOURCE_REF_RE = re.compile(r"\[([^\]\[]+\.(?:cs|js|jsx|ts|tsx|py|cpp|c|h|hpp|rs|go|java|kt|md|json|yaml|yml|sh|bat))(?::\d+(?:-\d+)?)?\]")


def _extract_sources(content: str, known_files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[r122] LLM 응답에서 [path/file:line] 형태 Sources 자동 추출 → 메타 sources 배열."""
    refs: List[Dict[str, Any]] = []
    seen = set()
    for m in _SOURCE_REF_RE.finditer(content):
        raw = m.group(0).strip("[]")
        path = m.group(1)
        line_part = raw[len(path):].lstrip(":")
        ls: Optional[int] = None
        le: Optional[int] = None
        if line_part:
            lm = re.match(r"(\d+)(?:-(\d+))?", line_part)
            if lm:
                ls = int(lm.group(1))
                le = int(lm.group(2)) if lm.group(2) else ls
        key = (path, ls, le)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"path": path, "line_start": ls, "line_end": le})
    # known_files에 없는 path는 그대로 두되 정규화
    return refs


# ─────────────────────────────────────────────
# 메인 파이프라인
# ─────────────────────────────────────────────

async def generate_wiki(
    git_url: str,
    project_id: str,
    branch: str = "main",
    clean_first: bool = True,
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Git 레포 → DeepWiki 스타일 위키 페이지 N+2개 자동 생성."""
    if not project_id:
        yield {"event": "error", "message": "project_id 필수"}
        return
    if not git_url:
        yield {"event": "error", "message": "git_url 필수"}
        return

    store = get_store()
    ollama = get_ollama()
    if not await ollama.ping():
        yield {"event": "error", "message": "Ollama 연결 실패 — Ollama 가 실행 중인지 확인 (ollama serve)"}
        return

    # [r133] 모델 설치 확인 — 안 깔린 모델로 시도하면 LLM 첫 호출에서 실패
    effective_model = model or settings.LLM_MODEL
    try:
        installed = await ollama.list_models()
        # 'qwen2.5-coder:32b' 가 'qwen2.5-coder:32b-instruct' 같은 변형으로 깔려있을 수도 있어 prefix 매치
        base = effective_model.split(":")[0]
        tag = effective_model.split(":", 1)[1] if ":" in effective_model else None
        exact = effective_model in installed
        loose = any(m == effective_model or m.startswith(effective_model + ":") or (tag and m.startswith(base + ":" + tag)) for m in installed)
        if not (exact or loose):
            msg = (
                f"❌ 모델 '{effective_model}' 가 Ollama 에 설치되어 있지 않습니다.\n"
                f"설치된 모델: {', '.join(installed[:20]) or '(없음)'}\n"
                f"해결: ollama pull {effective_model}"
            )
            print(f"[wiki_generator] {msg}")
            yield {"event": "error", "message": msg, "installed_models": installed[:30]}
            return
        else:
            yield {"event": "info", "message": f"✓ LLM 모델 '{effective_model}' 확인됨 ({len(installed)}개 설치됨)"}
    except Exception as e:
        yield {"event": "warn", "message": f"모델 목록 조회 실패(진행 계속): {e}"}

    # ─ 1) Git clone
    clone_dir = Path(settings.GIT_CLONE_DIR)
    clone_dir.mkdir(parents=True, exist_ok=True)
    repo_name = git_url.rstrip("/").split("/")[-1].replace(".git", "")
    repo_path = clone_dir / repo_name
    yield {"event": "stage", "stage": "clone", "message": f"Git clone: {git_url}"}
    try:
        if repo_path.exists() and clean_first:
            _safe_remove_repo(repo_path)
        if not repo_path.exists():
            attempt_log: List[str] = []
            def _on_attempt(b):
                attempt_log.append(b)
                print(f"[wiki_generator] clone attempt: branch='{b}'")
            try:
                used_branch = clone_with_fallback(
                    git_url, repo_path,
                    requested_branch=branch,
                    on_attempt=_on_attempt,
                )
                yield {"event": "info", "message": f"브랜치 '{used_branch}' 클론 성공 (시도: {' / '.join(attempt_log)})"}
            except git.exc.GitCommandError as gce:
                detail = (gce.stderr or gce.stdout or str(gce))
                if isinstance(detail, bytes):
                    detail = detail.decode("utf-8", errors="ignore")
                yield {"event": "error", "message": f"git clone 실패 ({gce.status}): {detail.strip()}", "tried_branches": attempt_log}
                return
            except Exception as ge:
                yield {"event": "error", "message": f"clone 실패: {type(ge).__name__}: {ge}", "tried_branches": attempt_log}
                return
    except Exception as e:
        yield {"event": "error", "message": f"clone 처리 실패: {type(e).__name__}: {e}"}
        return
    try:
        commit_hash = git.Repo(repo_path).head.commit.hexsha[:12]
    except Exception:
        commit_hash = None
    yield {"event": "clone_done", "commit": commit_hash}

    # ─ 2) 스캔 + 카테고리 분류
    yield {"event": "stage", "stage": "scan", "message": "파일 트리 분석 중..."}
    # [r132] Unity 프로젝트 감지 — 감지되면 LLM 프롬프트에 Unity addendum 추가
    unity_mode = _detect_unity_project(repo_path)
    if unity_mode:
        yield {"event": "info", "message": "🎮 Unity 프로젝트 감지됨 — MonoBehaviour·SerializeField·Coroutine·ScriptableObject 등 특화 분석 활성화"}
    categories = _scan_repo(repo_path)
    # 너무 작은 카테고리는 root로 머지
    if "project-config" not in categories:
        categories["project-config"] = {"title": "Project Configuration", "slug": "project-config", "priority": 999, "files": [], "total_files": 0}
    merged: List[str] = []
    for slug in list(categories.keys()):
        if slug != "project-config" and categories[slug]["total_files"] < 2:
            for f in categories[slug]["files"]:
                if len(categories["project-config"]["files"]) < 15:
                    categories["project-config"]["files"].append(f)
                categories["project-config"]["total_files"] += 1
            merged.append(slug)
            del categories[slug]
    if not categories.get("project-config", {}).get("files"):
        categories.pop("project-config", None)
    cat_count = len(categories)
    yield {"event": "scan_done", "category_count": cat_count, "merged": merged}

    # ─ 3) [r121→r131] repo·버전 관리 — is_latest=false 는 첫 페이지 성공 후로 지연
    repo_clean = re.sub(r"[^a-zA-Z0-9_-]", "_", repo_name)[:60] or "unknown"
    generation_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    gen_start_ts = time.time()
    # [r131] 0-페이지 안전망: 페이지 저장 성공이 한 번이라도 일어나야 기존 latest 를 강등
    is_latest_swapped = False
    collected_errors: List[Dict[str, Any]] = []
    successful_categories: List[Dict[str, Any]] = []
    failed_categories: List[Dict[str, Any]] = []

    def _swap_is_latest_once():
        """첫 페이지 저장 성공 시 한 번만 호출 — 기존 같은 repo 의 is_latest=false."""
        nonlocal is_latest_swapped
        if is_latest_swapped:
            return None
        try:
            store.client.table("deep_wiki_pages").update({"is_latest": False}) \
                .eq("project_id", project_id).eq("repo_name", repo_clean) \
                .neq("generation_id", generation_id).execute()
            is_latest_swapped = True
            return f"기존 '{repo_clean}' 버전 → is_latest=false (첫 새 페이지 저장 후 스왑)"
        except Exception as e:
            return f"is_latest 스왑 실패 (003 SQL 미실행?): {e}"

    # ─ 4) Architecture 페이지 먼저 (전체 시스템 다이어그램)
    yield {"event": "stage", "stage": "architecture", "message": f"🏛 Architecture 페이지 생성 중..."}
    cat_items_sorted = sorted(categories.values(), key=lambda x: x["priority"])
    try:
        arch_page = await _llm_generate_architecture_page(
            categories=cat_items_sorted,
            git_url=git_url,
            commit=commit_hash or "",
            repo_name=repo_name,
            model=model,
            unity_mode=unity_mode,
        )
        arch_id = f"dwp:{project_id}:{repo_clean}:{generation_id}:_architecture"
        store.client.table("deep_wiki_pages").upsert({
            "id": arch_id,
            "project_id": project_id,
            "repo_name": repo_clean,
            "generation_id": generation_id,
            "is_latest": True,
            "git_url": git_url,
            "git_commit": commit_hash,
            "slug": "_architecture",
            "title": "🏛 System Architecture",
            "parent_slug": None,
            "sort_order": -2,  # _overview(-1) 보다 위
            "summary": arch_page["summary"],
            "content": arch_page["content"],
            "meta": {
                "is_architecture": True,
                "category_count": cat_count,
                "generated_by": model or settings.LLM_MODEL,
                "sources": arch_page.get("sources") or [],
            },
        }).execute()
        # [r131] 첫 페이지 저장 성공 → 이제 안전하게 is_latest 스왑
        msg = _swap_is_latest_once()
        if msg:
            yield {"event": "info", "message": msg}
        yield {"event": "page_done", "slug": "_architecture", "title": "🏛 System Architecture"}
    except Exception as e:
        tb = traceback.format_exc()
        err_msg = f"Architecture 페이지 실패: {type(e).__name__}: {e}"
        # [r133] 콘솔에 풀 traceback — run.bat 에 보임
        print(f"[wiki_generator] ❌ ARCHITECTURE ERROR\n{tb}")
        yield {"event": "warn", "message": err_msg, "traceback": tb.splitlines()[-8:]}
        collected_errors.append({"slug": "_architecture", "error": err_msg, "traceback": tb.splitlines()[-12:]})

    # ─ 5) 카테고리별 페이지 (LLM 호출 N번)
    yield {"event": "stage", "stage": "generate", "message": f"LLM이 {cat_count}개 시스템 페이지 생성 중..."}
    created_pages: List[Dict[str, Any]] = []
    for idx, cat in enumerate(cat_items_sorted):
        yield {
            "event": "progress",
            "current": idx + 1,
            "total": cat_count,
            "category": cat["title"],
            "slug": cat["slug"],
            "files_in_cat": cat["total_files"],
        }
        try:
            page = await _llm_generate_category_page(cat=cat, model=model, unity_mode=unity_mode)
        except Exception as e:
            tb = traceback.format_exc()
            err_msg = f"LLM 실패 ({cat['slug']}): {type(e).__name__}: {e}"
            print(f"[wiki_generator] ❌ CATEGORY {cat['slug']} ERROR\n{tb}")
            yield {"event": "warn", "message": err_msg, "traceback": tb.splitlines()[-8:]}
            collected_errors.append({"slug": cat["slug"], "title": cat["title"], "error": err_msg, "traceback": tb.splitlines()[-12:]})
            failed_categories.append({"slug": cat["slug"], "title": cat["title"], "reason": f"LLM 실패: {type(e).__name__}"})
            continue
        page_id = f"dwp:{project_id}:{repo_clean}:{generation_id}:{cat['slug']}"
        try:
            store.client.table("deep_wiki_pages").upsert({
                "id": page_id,
                "project_id": project_id,
                "repo_name": repo_clean,
                "generation_id": generation_id,
                "is_latest": True,
                "git_url": git_url,
                "git_commit": commit_hash,
                "slug": cat["slug"],
                "title": page["title"],
                "parent_slug": None,
                "sort_order": cat["priority"],
                "summary": page["summary"],
                "content": page["content"],
                "meta": {
                    "category_title": cat["title"],
                    "total_files": cat["total_files"],
                    "files_used": [f["path"] for f in cat["files"]],
                    "sources": page.get("sources") or [],
                    "generated_by": model or settings.LLM_MODEL,
                },
            }).execute()
            # [r131] 첫 페이지 저장 성공 → is_latest 스왑
            msg = _swap_is_latest_once()
            if msg:
                yield {"event": "info", "message": msg}
            created_pages.append({"slug": cat["slug"], "title": page["title"], "summary": page["summary"]})
            successful_categories.append({"slug": cat["slug"], "title": cat["title"]})
            yield {"event": "page_done", "slug": cat["slug"], "title": page["title"], "summary": page["summary"]}
        except Exception as e:
            err_msg = f"DB 저장 실패 ({cat['slug']}): {e}"
            yield {"event": "warn", "message": err_msg}
            collected_errors.append({"slug": cat["slug"], "title": cat["title"], "error": err_msg})
            failed_categories.append({"slug": cat["slug"], "title": cat["title"], "reason": "DB 저장 실패"})

    # ─ 6) Overview 페이지 (인덱스)
    yield {"event": "stage", "stage": "overview", "message": "📘 Overview 페이지 생성 중..."}
    try:
        overview_content = _build_overview_page(git_url, commit_hash, created_pages, categories, repo_name)
        store.client.table("deep_wiki_pages").upsert({
            "id": f"dwp:{project_id}:{repo_clean}:{generation_id}:_overview",
            "project_id": project_id,
            "repo_name": repo_clean,
            "generation_id": generation_id,
            "is_latest": True,
            "git_url": git_url,
            "git_commit": commit_hash,
            "slug": "_overview",
            "title": "📘 Overview",
            "parent_slug": None,
            "sort_order": -1,
            "summary": f"{len(created_pages)}개 시스템 자동 위키 ({commit_hash or '?'})",
            "content": overview_content,
            "meta": {"is_overview": True, "page_count": len(created_pages) + 1, "repo": repo_clean, "generation": generation_id},
        }).execute()
        msg = _swap_is_latest_once()
        if msg:
            yield {"event": "info", "message": msg}
        yield {"event": "page_done", "slug": "_overview", "title": "📘 Overview"}
    except Exception as e:
        yield {"event": "warn", "message": f"Overview 페이지 실패: {e}"}

    # ─ 7) [r131] Generation Report — 항상 저장. 모든 LLM 실패해도 이게 있어서 트리에 뭔가 보임.
    yield {"event": "stage", "stage": "report", "message": "📋 Generation Report 생성 중..."}
    try:
        elapsed = int(time.time() - gen_start_ts)
        report_md = _build_generation_report(
            repo_name=repo_clean,
            git_url=git_url,
            commit=commit_hash,
            generation_id=generation_id,
            model=model or settings.LLM_MODEL,
            elapsed_sec=elapsed,
            total_categories=cat_count,
            successful=successful_categories,
            failed=failed_categories,
            errors=collected_errors,
            arch_ok=any(p.get("slug") == "_architecture" for p in [{"slug": e.get("slug")} for e in [{"slug": "_architecture"}]] if not any(err.get("slug") == "_architecture" for err in collected_errors)),
        )
        store.client.table("deep_wiki_pages").upsert({
            "id": f"dwp:{project_id}:{repo_clean}:{generation_id}:_report",
            "project_id": project_id,
            "repo_name": repo_clean,
            "generation_id": generation_id,
            "is_latest": True,
            "git_url": git_url,
            "git_commit": commit_hash,
            "slug": "_report",
            "title": "📋 Generation Report",
            "parent_slug": None,
            "sort_order": 999,  # 트리 맨 아래
            "summary": f"성공 {len(successful_categories)} · 실패 {len(failed_categories)} · {elapsed}s · {model or settings.LLM_MODEL}",
            "content": report_md,
            "meta": {
                "is_report": True,
                "elapsed_sec": elapsed,
                "model": model or settings.LLM_MODEL,
                "successful_count": len(successful_categories),
                "failed_count": len(failed_categories),
                "errors": collected_errors[:30],
            },
        }).execute()
        # 만약 위 모든 페이지가 실패했더라도 Report 라도 latest 로 만들기 — 사용자가 진단 가능
        msg = _swap_is_latest_once()
        if msg:
            yield {"event": "info", "message": msg}
        yield {"event": "page_done", "slug": "_report", "title": "📋 Generation Report"}
    except Exception as e:
        yield {"event": "warn", "message": f"Generation Report 실패: {e}"}

    total_pages = len(created_pages) + (1 if is_latest_swapped else 0)  # 대략치
    yield {
        "event": "done",
        "pages_created": len(created_pages) + 2,  # +arch +overview (성공한 경우)
        "successful_categories": len(successful_categories),
        "failed_categories": len(failed_categories),
        "is_latest_swapped": is_latest_swapped,
        "repo_name": repo_clean,
        "generation_id": generation_id,
        "elapsed_sec": int(time.time() - gen_start_ts),
    }


# ─────────────────────────────────────────────
# [r132] 기존 페이지 확장 — extend_wiki_page
# ─────────────────────────────────────────────

# 확장 타입별 prompt addendum
_EXTEND_PROMPTS: Dict[str, str] = {
    "deep_dive": (
        "위 문서를 **기술적 deep dive** 로 확장하세요. 다음 관점을 추가 분석:\n"
        "- 핵심 알고리즘의 시간/공간 복잡도\n"
        "- 디자인 패턴 명시 (싱글톤·Observer·Strategy·State 등)\n"
        "- 데이터 구조 선택 이유\n"
        "- 스레딩·동시성 모델 (있다면)\n"
        "- 메모리 할당 패턴 (Unity면 GC 압박·풀링)\n"
        "- 호출 흐름의 시퀀스 다이어그램 1~2개 추가\n"
    ),
    "performance": (
        "위 문서에 **성능·최적화** 섹션을 추가하세요:\n"
        "- 핫패스 식별 (매 프레임 호출되는 코드)\n"
        "- 잠재적 병목 (할당·복사·LINQ·boxing)\n"
        "- Unity 특화: Update vs FixedUpdate vs LateUpdate 부담\n"
        "- 풀링 적용 가능 부분, ScriptableObject 활용 가능성\n"
        "- 측정·프로파일링 권장 지점\n"
        "- 개선 안 2~3개 (코드 발췌 예시 포함)\n"
    ),
    "pitfalls": (
        "위 문서에 **주의사항·안티패턴·일반적 실수** 섹션을 추가하세요:\n"
        "- 코드에서 발견되는 잠재적 버그 (race condition·null·order of execution)\n"
        "- Unity 특화 함정 (Awake/Start 순서·OnDestroy 정리·코루틴 누수)\n"
        "- 흔한 오용 시나리오\n"
        "- 안전한 사용법 가이드\n"
    ),
    "examples": (
        "위 문서에 **사용 예시·코드 샘플** 섹션을 추가하세요:\n"
        "- 시스템을 외부에서 호출하는 미니멀한 코드 샘플 3개\n"
        "- 각 샘플은 csharp 코드블록으로 + 어떤 시나리오인지 설명\n"
        "- 발견된 실제 호출처를 Sources 인용으로 같이 표시\n"
        "- 가능하면 sequenceDiagram 추가\n"
    ),
    "testing": (
        "위 문서에 **테스트 전략** 섹션을 추가하세요:\n"
        "- 단위 테스트 가능한 메서드 식별\n"
        "- Mock/Stub 이 필요한 의존성\n"
        "- Unity Test Framework (PlayMode/EditMode) 적용 권장\n"
        "- 통합 테스트 시나리오 제안 (Given/When/Then 형식)\n"
        "- 테스트하기 어려운 부분 + 리팩토링 제안\n"
    ),
    "custom": "위 문서에 사용자 요청 사항대로 새로운 섹션을 추가하세요:\n\n{custom_prompt}",
}


async def extend_wiki_page(
    project_id: str,
    slug: str,
    extension_type: str,
    custom_prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """[r132] 기존 위키 페이지에 LLM 으로 새 섹션 추가.

    1. deep_wiki_pages 에서 slug 로 페이지 fetch
    2. 확장 타입별 prompt + 기존 본문 → LLM 호출
    3. 응답을 본문 끝에 `## 확장 분석 ({type}, {timestamp})` 헤더 + 내용으로 append
    4. meta.extensions 배열에 이력 push
    5. updated_at 갱신
    """
    store = get_store()
    ollama = get_ollama()

    yield {"event": "stage", "stage": "fetch", "message": "페이지 로드 중..."}
    try:
        res = (
            store.client.table("deep_wiki_pages")
            .select("*")
            .eq("project_id", project_id)
            .eq("slug", slug)
            .eq("is_latest", True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            yield {"event": "error", "message": f"슬러그 '{slug}' 페이지를 찾을 수 없습니다 (latest)"}
            return
        page = rows[0]
    except Exception as e:
        yield {"event": "error", "message": f"페이지 조회 실패: {e}"}
        return

    yield {"event": "info", "message": f"📄 {page.get('title', '?')} 로드 완료 — 본문 {len(page.get('content', ''))}자"}

    addendum = _EXTEND_PROMPTS.get(extension_type)
    if not addendum:
        yield {"event": "error", "message": f"알 수 없는 확장 타입: {extension_type}"}
        return
    if extension_type == "custom":
        if not custom_prompt or not custom_prompt.strip():
            yield {"event": "error", "message": "custom_prompt 가 비어있습니다"}
            return
        addendum = addendum.format(custom_prompt=custom_prompt.strip())

    # 본문 너무 길면 LLM context 초과 — 8000자 정도로 컷 (8000 chars ≈ 2000 tokens)
    existing_content = (page.get("content") or "")[:8000]
    title = page.get("title", "")

    user_msg = (
        f"# 확장 요청\n\n"
        f"아래는 기존 위키 페이지 '{title}' 의 본문입니다. 이 문서를 읽고 새로운 섹션을 추가하세요.\n\n"
        f"## 추가 지시\n\n{addendum}\n\n"
        f"## 출력 규칙\n"
        f"- 새로운 마크다운 섹션만 출력 (기존 본문 반복 금지)\n"
        f"- 한국어\n"
        f"- 모든 클래스·메서드 언급 시 [path/file.cs:N-M] Sources 인용\n"
        f"- Mermaid 다이어그램 활용 권장\n"
        f"- 코드에 없는 사실 만들지 말 것\n\n"
        f"---\n\n# 기존 본문\n\n{existing_content}"
    )

    system_msg = (
        "당신은 시니어 테크니컬 라이터입니다. 이미 작성된 코드 위키 페이지에 "
        "기술적으로 더 깊은 분석 섹션을 추가합니다. 기존 본문을 다시 쓰지 말고 "
        "오직 새로운 섹션만 마크다운으로 출력합니다. 한국어. Sources 인용 필수."
    )

    yield {"event": "stage", "stage": "llm", "message": "🤖 LLM 으로 확장 섹션 생성 중..."}
    try:
        chunks: List[str] = []
        async for d in ollama.chat_stream(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            model=model,
            temperature=0.25,
        ):
            chunks.append(d)
        new_section = "".join(chunks).strip()
        if not new_section:
            yield {"event": "error", "message": "LLM 이 빈 응답을 반환했습니다"}
            return
    except Exception as e:
        yield {"event": "error", "message": f"LLM 호출 실패: {type(e).__name__}: {e}"}
        return

    # 본문 append + 메타 이력
    now_iso = datetime.now().isoformat()
    type_label = {
        "deep_dive": "기술 Deep Dive",
        "performance": "성능·최적화",
        "pitfalls": "주의사항·안티패턴",
        "examples": "사용 예시",
        "testing": "테스트 전략",
        "custom": "맞춤 분석",
    }.get(extension_type, extension_type)
    appended_md = (
        f"\n\n---\n\n"
        f"## ✦ 확장 분석 — {type_label} ({now_iso[:19]})\n\n"
        f"_모델: {model or settings.LLM_MODEL}_\n\n"
        f"{new_section}\n"
    )
    new_content = (page.get("content") or "") + appended_md

    extensions = list((page.get("meta") or {}).get("extensions") or [])
    extensions.append({
        "type": extension_type,
        "label": type_label,
        "added_at": now_iso,
        "model": model or settings.LLM_MODEL,
        "prompt_excerpt": (custom_prompt or "")[:200] if extension_type == "custom" else None,
        "chars_added": len(new_section),
    })
    new_meta = dict(page.get("meta") or {})
    new_meta["extensions"] = extensions[-30:]  # 최근 30회만 보존

    yield {"event": "stage", "stage": "save", "message": "DB 저장 중..."}
    try:
        store.client.table("deep_wiki_pages").update({
            "content": new_content,
            "meta": new_meta,
        }).eq("id", page["id"]).execute()
        yield {
            "event": "done",
            "slug": slug,
            "title": title,
            "extension_type": extension_type,
            "chars_added": len(new_section),
            "total_extensions": len(extensions),
        }
    except Exception as e:
        yield {"event": "error", "message": f"저장 실패: {e}"}


def _build_generation_report(
    repo_name: str,
    git_url: str,
    commit: Optional[str],
    generation_id: str,
    model: str,
    elapsed_sec: int,
    total_categories: int,
    successful: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    arch_ok: bool,
) -> str:
    """[r131] Generation Report 마크다운. 어떤 카테고리가 성공/실패했는지 + 에러 메시지."""
    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    success_rate = (len(successful) / total_categories * 100) if total_categories else 0
    if len(successful) == 0 and not arch_ok:
        status_banner = "## ❌ 생성 실패 — 모든 페이지 LLM 실패"
        status_detail = "한 페이지도 저장되지 못했습니다. 아래 에러를 확인하고 모델/설정 점검 후 재시도."
    elif len(failed) == 0 and arch_ok:
        status_banner = "## ✅ 생성 성공"
        status_detail = f"모든 카테고리({total_categories}개) 가 정상 생성되었습니다."
    else:
        status_banner = "## ⚠ 부분 성공"
        status_detail = f"{len(successful)}/{total_categories} 카테고리 성공 ({success_rate:.0f}%). 실패 항목은 아래 참고."

    lines = [
        f"# 📋 Generation Report — {repo_name}",
        "",
        status_banner,
        "",
        status_detail,
        "",
        "## 🧾 메타",
        "",
        f"- **레포**: [{git_url}]({git_url})",
        f"- **커밋**: `{commit or '?'}`",
        f"- **생성 ID**: `{generation_id}`",
        f"- **LLM 모델**: `{model}`",
        f"- **소요 시간**: {elapsed_sec}초",
        f"- **완료 시각**: {today}",
        f"- **Architecture 페이지**: {'✅ 성공' if arch_ok else '❌ 실패'}",
        f"- **카테고리 성공**: {len(successful)} / {total_categories}",
        f"- **카테고리 실패**: {len(failed)}",
        "",
    ]
    if successful:
        lines.append("## ✅ 성공한 카테고리")
        lines.append("")
        for s in successful:
            lines.append(f"- **[{s['title']}](?slug={s['slug']})**")
        lines.append("")
    if failed:
        lines.append("## ❌ 실패한 카테고리")
        lines.append("")
        for f in failed:
            lines.append(f"- **{f['title']}** (`{f['slug']}`) — {f.get('reason', '?')}")
        lines.append("")
    if errors:
        lines.append("## 🪲 에러 상세")
        lines.append("")
        for e in errors[:20]:
            lines.append(f"### `{e.get('slug', '?')}`")
            lines.append("")
            lines.append("```")
            lines.append(str(e.get("error", "?"))[:500])
            lines.append("```")
            lines.append("")
    lines.extend([
        "## 🛠 대처",
        "",
        "1. **Ollama 콘솔 확인** — `run.bat` 창에서 LLM 호출 에러 로그",
        "2. **모델 로드 확인** — 32B 모델 첫 호출은 콜드 스타트(2~5분) 시간 필요. 다시 실행하면 빠름",
        "3. **모델 다운로드** — `ollama list` 로 설정한 모델이 있는지 확인. 없으면 `ollama pull <모델명>`",
        "4. **메모리 확인** — 32B 모델은 RAM 32GB+ 또는 VRAM 22GB+ 필요. 부족하면 14B / 7B 로 변경 (⚙ 설정)",
        "5. **재시도** — 좌측 ✦ 위키 자동 생성 다시 실행. 모델이 메모리에 올라가 있으면 빠르게 진행",
        "",
        "_이 페이지는 매 생성 시 자동으로 저장됩니다 — 결과 확인 + 진단용._",
    ])
    return "\n".join(lines)


def _build_overview_page(
    git_url: str,
    commit: Optional[str],
    pages: List[Dict[str, Any]],
    categories: Dict[str, Dict[str, Any]],
    repo_name: str,
) -> str:
    """Overview 페이지 마크다운."""
    total_files = sum(c["total_files"] for c in categories.values())
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 📘 {repo_name}",
        "",
        f"**Last indexed:** {today} (`{commit or '?'}`)  ",
        f"**Git 레포:** [{git_url}]({git_url})  ",
        f"**자동 생성 페이지:** {len(pages) + 2}개 (Architecture · Overview · {len(pages)} 시스템)  ",
        f"**분석 파일 총합:** {total_files}개  ",
        f"**생성 모델:** `{settings.LLM_MODEL}`",
        "",
        "이 Deep Wiki는 LLM이 코드를 분석해 자동 작성한 1차 문서입니다. 좌측 트리에서 시스템 페이지를 열어 아키텍처·핵심 컴포넌트·데이터 흐름·코드 발췌·Sources를 확인하세요. 모든 페이지에 인용 출처(`[path/file.cs:N-M]`)가 포함됩니다.",
        "",
        "## 📊 시스템 맵",
        "",
        "```mermaid",
        "graph TB",
        f"    ROOT[\"📘 {repo_name}<br/>{total_files} files\"]",
    ]
    # [r125 #1·#2] 실제 생성된 페이지(by_slug)만 Subsystems / 시스템맵에 포함 — 404 링크 제거
    by_slug = {p["slug"]: p for p in pages}
    cat_items_all = sorted(categories.values(), key=lambda c: c["priority"])
    cat_items = [c for c in cat_items_all if c["slug"] in by_slug]
    skipped = [c for c in cat_items_all if c["slug"] not in by_slug]
    for cat in cat_items:
        node_id = re.sub(r"[^A-Za-z0-9_]", "_", cat["slug"])
        title = by_slug.get(cat["slug"], {}).get("title") or cat["title"]
        # 짧은 라벨
        short = title.split("(")[0].strip()
        lines.append(f"    ROOT --> {node_id}[\"{short}<br/>{cat['total_files']} files\"]")
    lines.extend(["    classDef root fill:#c96442,color:#fff,stroke:#a04a2a;", "    class ROOT root;", "```", ""])

    lines.extend([
        "## 📁 Subsystems",
        "",
        "| Subsystem | Pages | Files | Summary |",
        "|-----------|-------|-------|---------|",
    ])
    for cat in cat_items:
        slug = cat["slug"]
        title = by_slug.get(slug, {}).get("title") or cat["title"]
        summary = by_slug.get(slug, {}).get("summary") or ""
        if len(summary) > 80:
            summary = summary[:80] + "…"
        lines.append(f"| **[{title}](?slug={slug})** | [`{slug}`](?slug={slug}) | {cat['total_files']} | {summary} |")

    # [r125 #1] LLM 실패로 생성 안 된 카테고리 명시 (혼동 방지)
    if skipped:
        lines.extend([
            "",
            "### ⚠ 생성되지 않은 카테고리",
            "",
            "_(LLM 호출 실패 또는 타임아웃. 위키 자동 생성을 다시 실행하면 재시도됩니다.)_",
            "",
        ])
        for cat in skipped:
            lines.append(f"- ❌ **{cat['title']}** ({cat['total_files']} 파일) — `{cat['slug']}`")

    lines.extend([
        "",
        "## 🔗 Pages",
        "",
        "- 🏛 **[System Architecture](?slug=_architecture)** — 전체 시스템 관계 다이어그램 + 데이터 흐름",
    ])
    for p in pages:
        if p["slug"].startswith("_"):
            continue
        lines.append(f"- 📘 **[{p['title']}](?slug={p['slug']})** — {(p.get('summary') or '')[:100]}")

    lines.extend([
        "",
        "## 💡 Usage",
        "",
        "- 좌측 트리에서 페이지 클릭 → 시스템 상세 분석 (Overview · System Architecture · Core Components · Data Flow · Key Features · Code References · Sources)",
        "- 본문 Mermaid 다이어그램은 자동 렌더링",
        "- 우측 목차(On this page)로 페이지 내 빠른 이동",
        "- 좌측 검색바로 페이지 제목 필터링",
        "- 📦 레포 드롭다운으로 다른 레포 전환, 🕐 버전 드롭다운으로 옛 분석 결과 확인",
        "- AI Agent 챗봇에서 \"PlayerManager가 뭐 하는 클래스야?\" 같은 질문도 가능 (이 위키 페이지가 컨텍스트로 사용됨)",
    ])
    return "\n".join(lines)
