"""Run a user Gradio script and keep its server process alive."""

from __future__ import annotations

import os
import runpy
import sys
import time


def _is_demo(value: object) -> bool:
    return callable(getattr(value, "launch", None)) and hasattr(value, "is_running")


def _find_demo(namespace: dict[str, object]) -> object | None:
    demo = namespace.get("demo")
    if _is_demo(demo):
        return demo

    found: list[object] = []
    seen: set[int] = set()
    for value in namespace.values():
        if not _is_demo(value) or id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
    if len(found) == 1:
        return found[0]
    return None


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: runner.py <app.py>", file=sys.stderr)
        sys.exit(2)

    script = os.path.abspath(sys.argv[1])
    os.chdir(os.path.dirname(script))
    sys.argv = [script]
    namespace = runpy.run_path(script, run_name="__main__")

    demo = _find_demo(namespace)
    if demo is None:
        print(
            "sandpox: 没有找到可启动的 Gradio 应用。请定义 demo = gr.Interface(...) 或 demo = gr.Blocks()。",
            file=sys.stderr,
        )
        sys.exit(1)

    if not demo.is_running:
        demo.launch(
            share=False,
            inline=False,
            inbrowser=False,
            prevent_thread_lock=True,
        )

    if not demo.is_running:
        print("sandpox: Gradio 启动后立即退出了。", file=sys.stderr)
        sys.exit(1)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
