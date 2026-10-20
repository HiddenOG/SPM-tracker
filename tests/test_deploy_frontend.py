"""The new front end only exists once it is built, and frontend/dist is not in
git - so every deploy has to build it. If that step went missing, the old
app would still redirect people to /app/... and every page would 404."""
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_railway_builds_the_front_end():
    toml = (ROOT / "railway.toml").read_text(encoding="utf-8")
    assert 'builder = "nixpacks"' in toml          # so nixpacks.toml is what runs
    nix = (ROOT / "nixpacks.toml").read_text(encoding="utf-8")
    assert "nodejs_20" in nix
    assert "cd frontend && npm ci" in nix and "npm run build" in nix
    # Vite and TypeScript are dev dependencies; a production install would skip them.
    assert "--include=dev" in nix
    # "..." keeps the Python provider's own packages and build steps.
    assert '"...", "tesseract"' in nix and 'cmds = ["...",' in nix
    # The system packages the PDF and OCR code needs are still there.
    for pkg in ("tesseract", "poppler_utils", "dejavu_fonts"):
        assert pkg in nix, pkg


def test_the_docker_image_builds_it_too():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:20-slim AS frontend" in docker
    assert "COPY --from=frontend /frontend/dist ./frontend/dist" in docker
    assert '"--workers", "1"' in docker


def test_what_is_built_is_not_committed_but_what_builds_it_is():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "frontend/dist/" in ignore and "node_modules/" in ignore
    for name in ("package.json", "package-lock.json", "vite.config.ts", "index.html", "tsconfig.json"):
        assert (ROOT / "frontend" / name).exists(), name


def test_the_browser_gets_small_images_and_a_favicon():
    """The sign-in screen once waited on a 450KB logo it shows at 48px."""
    web = ROOT / "data_fl" / "web"
    for name, most in (("logo-256.png", 30_000), ("favicon.ico", 30_000), ("apple-touch-icon.png", 30_000)):
        f = web / name
        assert f.is_file() and f.stat().st_size < most, name
    # The quotation PDFs keep the full-size original.
    assert (ROOT / "data_fl" / "Gemini_Generated_Image_z6jc3vz6jc3vz6jc.png").is_file()
    for page in ("web/index.html", "web/messages.html", "frontend/index.html"):
        assert 'href="/favicon.ico"' in (ROOT / page).read_text(encoding="utf-8"), page
    # The photo behind the sign-in is gone; the moving backdrop replaced it.
    assert "bg.jpg" not in (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    assert 'id="auth-bg"' in (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def test_the_docker_context_leaves_out_secrets_and_customer_files():
    di = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for entry in (".env", "venv/", "data/", "frontend/node_modules/", "frontend/dist/"):
        assert entry in di, entry
