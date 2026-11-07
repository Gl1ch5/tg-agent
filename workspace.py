import asyncio
import re
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import httpx
from config import WORKSPACES_DIR, SKILLS_DIR


def sanitize_project_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", name.strip())
    return clean[:60] or "default"


class WorkspaceManager:
    """
    Отвечает за выделение изолированной папки под каждый проект,
    хранение памяти проекта (.agent_memory.md), локальные Git-чекпоинты
    для мгновенного отката (↶ откатить) и интеграцию с GitHub через Classic Token.
    """

    def get_project_dir(self, project_name: str) -> Path:
        slug = sanitize_project_name(project_name)
        p = WORKSPACES_DIR / slug
        p.mkdir(parents=True, exist_ok=True)
        return p

    def list_projects(self) -> List[str]:
        if not WORKSPACES_DIR.exists():
            return ["default"]
        dirs = sorted([d.name for d in WORKSPACES_DIR.iterdir() if d.is_dir()])
        if not dirs:
            self.get_project_dir("default")
            return ["default"]
        return dirs

    async def ensure_project_initialized(self, project_name: str) -> Path:
        p = self.get_project_dir(project_name)
        mem_file = p / ".agent_memory.md"
        if not mem_file.exists():
            mem_file.write_text(
                f"# Память проекта: {project_name}\n"
                "- Здесь хранятся важные архитектурные заметки, стек и решения по проекту.\n",
                encoding="utf-8",
            )
        git_dir = p / ".git"
        if not git_dir.exists():
            await self._run_git(p, ["init"])
            await self._run_git(p, ["config", "user.email", "agent@local.bot"])
            await self._run_git(p, ["config", "user.name", "AI Agent"])
            await self.create_checkpoint(project_name, "Initial project state")
        return p

    async def _run_git(self, cwd: Path, args: List[str]) -> Tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()

    async def create_checkpoint(self, project_name: str, label: str = "checkpoint") -> str:
        """Создает локальный Git-коммит состояния папки проекта и возвращает SHA."""
        p = await self.ensure_project_initialized(project_name)
        await self._run_git(p, ["add", "-A"])
        await self._run_git(p, ["commit", "--allow-empty", "-m", f"[agent-checkpoint] {label}"])
        _, sha = await self._run_git(p, ["rev-parse", "HEAD"])
        return sha

    async def rollback_to_checkpoint(self, project_name: str, commit_sha: str) -> Tuple[bool, str]:
        """Откатывает все файлы папки проекта к указанному чекпоинту."""
        if not commit_sha:
            return False, "Чекпоинт не найден."
        p = self.get_project_dir(project_name)
        code1, out1 = await self._run_git(p, ["reset", "--hard", commit_sha])
        code2, out2 = await self._run_git(p, ["clean", "-fd"])
        if code1 == 0:
            return True, f"Файлы проекта откачены к состоянию `{commit_sha[:7]}`."
        return False, f"Ошибка отката: {out1} {out2}"

    def read_memory(self, project_name: str) -> str:
        p = self.get_project_dir(project_name)
        mem_file = p / ".agent_memory.md"
        if mem_file.exists():
            return mem_file.read_text(encoding="utf-8", errors="replace")[:3000]
        return ""

    def update_memory(self, project_name: str, content: str, append: bool = True) -> str:
        p = self.get_project_dir(project_name)
        mem_file = p / ".agent_memory.md"
        if append and mem_file.exists():
            existing = mem_file.read_text(encoding="utf-8", errors="replace")
            new_text = existing.rstrip() + "\n" + content.strip() + "\n"
        else:
            new_text = content.strip() + "\n"
        mem_file.write_text(new_text, encoding="utf-8")
        return "Память проекта (.agent_memory.md) успешно обновлена."

    # --- Система скиллов (Skills) с экономией токенов ---
    def list_skills_compact(self) -> str:
        """Возвращает только имена и 1-строчные описания скиллов для системного промпта."""
        items = []
        for f in sorted(SKILLS_DIR.glob("*.md")):
            text = f.read_text(encoding="utf-8", errors="replace")
            name = f.stem
            desc = "Пользовательский навык"
            for line in text.splitlines()[:10]:
                if line.lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
                    break
            items.append(f"- `{name}`: {desc}")
        return "\n".join(items) if items else "Нет сохраненных скиллов."

    def load_skill(self, name: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_\-]", "", name)
        path = SKILLS_DIR / f"{clean}.md"
        if not path.exists():
            return f"Скилл '{clean}' не найден."
        return path.read_text(encoding="utf-8", errors="replace")

    def save_skill(self, name: str, description: str, instructions: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", name.strip().lower())
        path = SKILLS_DIR / f"{clean}.md"
        body = (
            f"---\n"
            f"name: {clean}\n"
            f"description: {description.strip()}\n"
            f"---\n\n"
            f"{instructions.strip()}\n"
        )
        path.write_text(body, encoding="utf-8")
        return f"Скилл `{clean}` успешно сохранен в папку skills/."

    # --- Интеграция с GitHub через Classic Token ---
    async def list_github_repos(self, github_token: str, query: str = "") -> List[Dict[str, Any]]:
        if not github_token:
            return []
        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                "https://api.github.com/user/repos?sort=updated&per_page=50",
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            repos = resp.json()
            if query:
                q = query.lower()
                repos = [r for r in repos if q in r.get("full_name", "").lower() or q in (r.get("description") or "").lower()]
            return [
                {
                    "name": r["name"],
                    "full_name": r["full_name"],
                    "private": r.get("private", False),
                    "description": r.get("description") or "Без описания",
                    "clone_url": r["clone_url"],
                    "html_url": r["html_url"],
                }
                for r in repos[:25]
            ]

    async def create_github_repo(
        self, github_token: str, repo_name: str, private: bool = True, description: str = "Created by Telegram AI Agent"
    ) -> Tuple[bool, str, Optional[str]]:
        if not github_token:
            return False, "Не задан GitHub Classic Token.", None
        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        payload = {
            "name": sanitize_project_name(repo_name),
            "private": private,
            "description": description,
            "auto_init": True,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post("https://api.github.com/user/repos", headers=headers, json=payload)
            if resp.status_code not in (200, 201):
                return False, f"Ошибка GitHub API ({resp.status_code}): {resp.text[:300]}", None
            data = resp.json()
            full_name = data["full_name"]
            ok, msg = await self.clone_github_repo(github_token, full_name, data["name"])
            return ok, f"Репозиторий `{full_name}` создан! {msg}", data["name"]

    async def list_github_branches(self, github_token: str, full_repo_name: str) -> List[str]:
        if not github_token or "/" not in full_repo_name:
            return ["main", "master"]
        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{full_repo_name}/branches?per_page=30",
                    headers=headers,
                )
                if resp.status_code == 200:
                    branches = [b["name"] for b in resp.json() if "name" in b]
                    return branches or ["main"]
        except Exception:
            pass
        return ["main", "master"]

    async def clone_github_repo(
        self, github_token: str, full_repo_name: str, target_project_name: Optional[str] = None, branch: Optional[str] = None
    ) -> Tuple[bool, str]:
        project_slug = sanitize_project_name(target_project_name or full_repo_name.split("/")[-1])
        target_dir = WORKSPACES_DIR / project_slug
        if target_dir.exists() and any(target_dir.iterdir()):
            shutil.rmtree(target_dir)

        if github_token:
            clone_url = f"https://{github_token}@github.com/{full_repo_name}.git"
        else:
            clone_url = f"https://github.com/{full_repo_name}.git"

        cmd = ["git", "clone"]
        if branch:
            cmd.extend(["-b", branch])
        cmd.extend([clone_url, str(target_dir)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            err = out.decode("utf-8", errors="replace")
            if github_token:
                err = err.replace(github_token, "***")
            return False, f"Ошибка клонирования: {err}"

        await self.ensure_project_initialized(project_slug)
        branch_info = f" (ветка {branch})" if branch else ""
        return True, f"Склонировано в проект `{project_slug}`{branch_info}."

    async def push_to_github(self, project_name: str, commit_message: str = "Update from AI Agent") -> str:
        p = self.get_project_dir(project_name)
        await self._run_git(p, ["add", "-A"])
        await self._run_git(p, ["commit", "-m", commit_message])
        code, out = await self._run_git(p, ["push", "-u", "origin", "HEAD"])
        if code == 0:
            return f"Успешно отправлено (git push): {out or 'OK'}"
        return f"Ошибка git push: {out}"


workspace = WorkspaceManager()
