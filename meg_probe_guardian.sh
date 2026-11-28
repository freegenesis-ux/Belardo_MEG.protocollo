#!/usr/bin/env bash
# Sonda Guardian + Alto Calore: matrice indirizzo x User-Agent. Gira uguale su Termux e su GitHub Actions.
# Uso: bash meg-probe-guardian.sh   (scrive meg-probe-guardian-risultati.txt)
OUT="meg-probe-guardian-risultati.txt"
TMPD="$(mktemp -d "${TMPDIR:-$HOME}/mpg.XXXXXX")" || exit 1
trap 'rm -rf "$TMPD"' EXIT
UA_CHROME="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
UA_SAFARI="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
UA_BOT="MEG-Protocol/1.0 (+https://github.com/freegenesis-ux/Belardo_MEG.protocollo)"
UA_FEED="Feedly/1.0 (+http://www.feedly.com/fetcher.html; like FeedFetcher-Google)"
UA_CURL="curl/8.5.0"
GU=(
"https://www.theguardian.com/world/rss"
"https://www.theguardian.com/international/rss"
"https://www.theguardian.com/world/europe-news/rss"
"https://www.theguardian.com/uk/rss"
"https://www.theguardian.com/world/rss?CMP=rss"
"https://feeds.theguardian.com/theguardian/world/rss"
)
AC=("https://www.altocalore.it/feed" "https://www.altocalore.it/feed/")
{ echo "MEG probe Guardian/AltoCalore  $(date -u +%FT%TZ)"
  echo "IP uscita: $(curl -sS -m 10 https://ipinfo.io/json 2>/dev/null | tr -d '\n ' | cut -c1-200)"
  printf '%-4s %-5s %-6s %-5s %-7s %s\n' HTTP NOTIZ BYTE SEC UA URL; } | tee "$OUT"
probe() { # url uaname ua
  B="$TMPD/b"; : > "$B"
  M="$(curl -sS -L -m 25 --max-redirs 5 -A "$3" -H 'Accept: application/rss+xml, application/xml;q=0.9, */*;q=0.8' -o "$B" -w '%{http_code}|%{time_total}|%{size_download}' "$1" 2>/dev/null)"
  code="${M%%|*}"; r="${M#*|}"; t="${r%%|*}"; sz="${r#*|}"; [ -n "$code" ] || code=000
  n="$(grep -o -i -E '<item[ >]|<entry[ >]' "$B" | wc -l | tr -d ' ')"
  printf '%-4s %-5s %-6s %-5.1f %-7s %s\n' "$code" "$n" "${sz:-0}" "${t:-0}" "$2" "$(echo "$1" | cut -c1-62)" | tee -a "$OUT"
  if [ "$n" = 0 ] && [ -s "$B" ]; then echo "      corpo: $(head -c 150 "$B" | tr '\n\r' '  ')" | tee -a "$OUT"; fi
}
for u in "${GU[@]}"; do for p in "chrome:$UA_CHROME" "safari:$UA_SAFARI" "bot:$UA_BOT" "feedly:$UA_FEED" "curl:$UA_CURL"; do probe "$u" "${p%%:*}" "${p#*:}"; done; done
echo "--- Alto Calore: 3 prove ripetute per vedere l'intermittenza ---" | tee -a "$OUT"
for i in 1 2 3; do for u in "${AC[@]}"; do for p in "chrome:$UA_CHROME" "safari:$UA_SAFARI"; do probe "$u" "${p%%:*}" "${p#*:}"; sleep 2; done; done; done
echo "Salvato in: $PWD/$OUT"
