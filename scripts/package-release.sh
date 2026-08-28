#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
evidence_dir="${NIGHTINGALE_RELEASE_EVIDENCE_DIR:-}"
pdf="${NIGHTINGALE_PDF_OUTPUT:-$root/output/pdf/Nightingale_Technical_Brief.pdf}"
pdf_binding="$pdf.binding.json"
video="${NIGHTINGALE_DEMO_VIDEO:-$root/output/demo/Nightingale_Final_Demo_EN.mp4}"
demo_srt="${NIGHTINGALE_DEMO_SRT:-$root/output/demo/Nightingale_Final_Demo_EN.srt}"
demo_script="${NIGHTINGALE_DEMO_SCRIPT:-$root/output/demo/Nightingale_Final_Demo_Script.en.md}"
demo_metadata="${NIGHTINGALE_DEMO_METADATA:-$root/output/demo/Nightingale_Final_Demo_EN_metadata.json}"
demo_contact_sheet="${NIGHTINGALE_DEMO_CONTACT_SHEET:-$root/output/demo/Nightingale_Final_Demo_EN_contact_sheet.png}"
demo_sha256="${NIGHTINGALE_DEMO_SHA256:-$root/output/demo/Nightingale_Final_Demo_EN_SHA256.txt}"
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
  "$evidence_dir/release-verification-complete.txt" \
  "$evidence_dir/verify-release.log" \
  "$evidence_dir/release-candidate.txt" \
  "$pdf" \
  "$pdf_binding" \
  "$video" \
  "$demo_srt" \
  "$demo_script" \
  "$demo_metadata" \
  "$demo_contact_sheet" \
  "$demo_sha256"; do
  if [[ ! -s "$required" ]]; then
    echo "Required release artifact is missing or empty: $required" >&2
    exit 1
  fi
done

python3 scripts/validate_release_evidence.py \
  --evidence-dir "$evidence_dir" \
  --expected-commit "$commit" \
  --pdf "$pdf"

stage="$delivery_root/Nightingale-72h-$short"
bundle="$delivery_root/Nightingale-72h-$short.zip"
if [[ -e "$stage" || -e "$bundle" || -e "$bundle.sha256" ]]; then
  echo "Refusing to overwrite an existing release path for $short." >&2
  exit 1
fi

mkdir -p "$stage/release-evidence" "$stage/artifacts"
git archive --format=zip --prefix=nightingale/ \
  --output="$stage/nightingale-source-$short.zip" HEAD
git bundle create "$stage/nightingale-history-$short.bundle" --all
git bundle verify "$stage/nightingale-history-$short.bundle" >/dev/null
cp "$evidence_dir"/* "$stage/release-evidence/"
cp "$pdf" "$stage/artifacts/Nightingale_Technical_Brief.pdf"
cp "$pdf_binding" \
  "$stage/artifacts/Nightingale_Technical_Brief.pdf.binding.json"
cp "$video" "$stage/artifacts/Nightingale_Demo.mp4"
cp "$demo_srt" "$stage/artifacts/Nightingale_Demo.en.srt"
cp "$demo_script" "$stage/artifacts/Nightingale_Demo_Script.en.md"
cp "$demo_metadata" "$stage/artifacts/Nightingale_Demo.metadata.json"
cp "$demo_contact_sheet" "$stage/artifacts/Nightingale_Demo_contact_sheet.png"
(
  cd "$stage/artifacts"
  shasum -a 256 \
    Nightingale_Demo.mp4 \
    Nightingale_Demo.en.srt \
    Nightingale_Demo_Script.en.md \
    Nightingale_Demo.metadata.json \
    Nightingale_Demo_contact_sheet.png > Nightingale_Demo_SHA256.txt
)

cat > "$stage/RELEASE_MANIFEST.txt" <<EOF
Nightingale 72-hour synthetic healthcare candidate
source_commit=$commit
source_archive=nightingale-source-$short.zip
git_history_bundle=nightingale-history-$short.bundle
release_evidence=release-evidence/
technical_brief=artifacts/Nightingale_Technical_Brief.pdf
technical_brief_evidence_binding=artifacts/Nightingale_Technical_Brief.pdf.binding.json
demo_video=artifacts/Nightingale_Demo.mp4
demo_language=en
demo_narration=false
demo_subtitles=burned-in+artifacts/Nightingale_Demo.en.srt
demo_script=artifacts/Nightingale_Demo_Script.en.md
demo_metadata=artifacts/Nightingale_Demo.metadata.json
demo_contact_sheet=artifacts/Nightingale_Demo_contact_sheet.png
demo_checksums=artifacts/Nightingale_Demo_SHA256.txt
data_boundary=synthetic_only
remote_publication=not_included
EOF

(
  cd "$stage"
  shasum -a 256 \
    "nightingale-source-$short.zip" \
    "nightingale-history-$short.bundle" \
    artifacts/Nightingale_Technical_Brief.pdf \
    artifacts/Nightingale_Technical_Brief.pdf.binding.json \
    artifacts/Nightingale_Demo.mp4 \
    artifacts/Nightingale_Demo.en.srt \
    artifacts/Nightingale_Demo_Script.en.md \
    artifacts/Nightingale_Demo.metadata.json \
    artifacts/Nightingale_Demo_contact_sheet.png \
    artifacts/Nightingale_Demo_SHA256.txt \
    release-evidence/* > SHA256SUMS.txt
)
(
  cd "$delivery_root"
  zip -q -r "$(basename "$bundle")" "$(basename "$stage")"
)
shasum -a 256 "$bundle" > "$bundle.sha256"
unzip -tq "$bundle" >/dev/null

printf '%s\n' "$bundle" "$bundle.sha256"
