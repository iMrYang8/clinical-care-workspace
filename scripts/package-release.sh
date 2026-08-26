#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
evidence_dir="${NIGHTINGALE_RELEASE_EVIDENCE_DIR:-}"
pdf="${NIGHTINGALE_PDF_OUTPUT:-$root/output/pdf/Nightingale_Technical_Brief.pdf}"
video="${NIGHTINGALE_DEMO_VIDEO:-$root/output/demo/Nightingale_Demo.mp4}"
delivery_root="${NIGHTINGALE_DELIVERY_ROOT:-$(dirname "$root")/release}"

if [[ -z "$evidence_dir" ]]; then
  echo "Set NIGHTINGALE_RELEASE_EVIDENCE_DIR to the exact verify-release output." >&2
  exit 2
fi
evidence_dir="$(cd "$evidence_dir" && pwd -P)"
cd "$root"

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "Release packaging requires a clean Git worktree." >&2
  git status --short >&2
  exit 1
fi

commit="$(git rev-parse HEAD)"
short="$(git rev-parse --short=12 HEAD)"
recorded_commit="$(cat "$evidence_dir/release-commit.txt")"
if [[ "$recorded_commit" != "$commit" ]]; then
  echo "Evidence commit $recorded_commit does not match checkout $commit." >&2
  exit 1
fi

for required in \
  "$evidence_dir/glance-benchmark.json" \
  "$evidence_dir/ffmpeg-container-version.txt" \
  "$evidence_dir/verified-backend-image-id.txt" \
  "$evidence_dir/release-candidate.txt" \
  "$pdf" \
  "$video"; do
  if [[ ! -s "$required" ]]; then
    echo "Required release artifact is missing or empty: $required" >&2
    exit 1
  fi
done

stage="$delivery_root/Nightingale-72h-$short"
bundle="$delivery_root/Nightingale-72h-$short.zip"
if [[ -e "$stage" || -e "$bundle" || -e "$bundle.sha256" ]]; then
  echo "Refusing to overwrite an existing release path for $short." >&2
  exit 1
fi

mkdir -p "$stage/release-evidence" "$stage/artifacts"
git archive --format=zip --prefix=nightingale/ \
  --output="$stage/nightingale-source-$short.zip" HEAD
cp "$evidence_dir"/* "$stage/release-evidence/"
cp "$pdf" "$stage/artifacts/Nightingale_Technical_Brief.pdf"
cp "$video" "$stage/artifacts/Nightingale_Demo.mp4"

cat > "$stage/RELEASE_MANIFEST.txt" <<EOF
Nightingale 72-hour synthetic healthcare candidate
source_commit=$commit
source_archive=nightingale-source-$short.zip
release_evidence=release-evidence/
technical_brief=artifacts/Nightingale_Technical_Brief.pdf
demo_video=artifacts/Nightingale_Demo.mp4
data_boundary=synthetic_only
remote_publication=not_included
EOF

(
  cd "$stage"
  shasum -a 256 \
    "nightingale-source-$short.zip" \
    artifacts/Nightingale_Technical_Brief.pdf \
    artifacts/Nightingale_Demo.mp4 \
    release-evidence/* > SHA256SUMS.txt
)
(
  cd "$delivery_root"
  zip -q -r "$(basename "$bundle")" "$(basename "$stage")"
)
shasum -a 256 "$bundle" > "$bundle.sha256"
unzip -tq "$bundle" >/dev/null

printf '%s\n' "$bundle" "$bundle.sha256"
