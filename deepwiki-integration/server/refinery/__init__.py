"""[r223] 연구 정련소 (Research Refinery).

raw 연구 문서를 vault → 분해 → 분류 → 결재 → 정의서 → 위키/문서 트리 → WBS/태스크
까지 한 페이지에서 완결시키는 모듈. r170~r222 인프라 재사용.

모듈:
- _author_guard: 모든 산출물에 작성자 필수 강제 (422)
- decomposer: vault 청크 분해 → 노드 트리 (LLM 분할 호출, SSE)
- classifier: AI 분류 추천 (노드 묶음)
- composer: 트리 + 본문 자동 작성 (옵시디언 링크)
- linker: [[]] 후처리 + 백링크
- work_proposer: WBS/태스크/이슈 제안

스펙: docs/연구정련소-스펙.md v0.2
"""
