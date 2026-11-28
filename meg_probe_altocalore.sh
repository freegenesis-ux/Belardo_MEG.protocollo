#!/usr/bin/env bash
# Sonda Alto Calore: varia intestazioni, metodo e indirizzo per capire il 415 visto dal runner.
OUT="meg-probe-altocalore-risultati.txt"
TMPD="$(mktemp -d "${TMPDIR:-$HOME}/mpa.XXXXXX")" || exit 1; trap 'rm -rf "$TMPD"' EXIT
UA_CH="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
UA_BOT="MEG-Protocol/1.0 (+https://github.com/freegenesis-ux/Belardo_MEG.protocollo)"
{ echo "MEG probe Alto Calore  $(date -u +%FT%TZ)"; echo "IP: $(curl -sS -m 10 https://ipinfo.io/ip 2>/dev/null)"
  printf '%-4s %-5s %-6s %-5s %s\n' HTTP NOTIZ BYTE SEC PROVA; } | tee "$OUT"
p() { # etichetta url ua [altri argomenti curl...]
  lab="$1"; u="$2"; ua="$3"; shift 3; B="$TMPD/b"; : > "$B"
  M="$(curl -sS -L -m 30 -A "$ua" "$@" -o "$B" -D "$TMPD/h" -w '%{http_code}|%{time_total}|%{size_download}' "$u" 2>/dev/null)"
  c="${M%%|*}"; r="${M#*|}"; t="${r%%|*}"; s="${r#*|}"; [ -n "$c" ] || c=000
  n="$(grep -o -i -E '<item[ >]|<entry[ >]' "$B" | wc -l | tr -d ' ')"
  printf '%-4s %-5s %-6s %-5.1f %s\n' "$c" "$n" "${s:-0}" "${t:-0}" "$lab" | tee -a "$OUT"
  if [ "$n" = 0 ]; then echo "      server: $(grep -i -E '^(server|content-type|cf-ray|x-sucuri|x-powered-by):' "$TMPD/h" | tr -d '\r' | tr '\n' ';' | cut -c1-140)"; echo "      corpo: $(head -c 120 "$B" | tr '\n\r' '  ')"; fi | tee -a "$OUT"
}
F=https://www.altocalore.it/feed
p "chrome, senza Accept"            $F "$UA_CH"
p "chrome, Accept */*"              $F "$UA_CH" -H 'Accept: */*'
p "chrome, Accept html"             $F "$UA_CH" -H 'Accept: text/html,application/xhtml+xml'
p "bot MEG, senza Accept"           $F "$UA_BOT"
p "curl, senza Accept"              $F "curl/8.5.0"
p "chrome + Accept-Language it"     $F "$UA_CH" -H 'Accept-Language: it-IT,it;q=0.9' -H 'Accept: */*'
p "chrome HTTP/1.1"                 $F "$UA_CH" --http1.1 -H 'Accept: */*'
p "chrome, ?feed=rss2"              "https://www.altocalore.it/?feed=rss2" "$UA_CH" -H 'Accept: */*'
p "chrome, /feed/atom"              "https://www.altocalore.it/feed/atom" "$UA_CH" -H 'Accept: */*'
p "chrome, /rss"                    "https://www.altocalore.it/rss" "$UA_CH" -H 'Accept: */*'
p "chrome, wp-json posts"           "https://www.altocalore.it/wp-json/wp/v2/posts?per_page=10" "$UA_CH" -H 'Accept: */*'
p "chrome, home"                    "https://www.altocalore.it/" "$UA_CH" -H 'Accept: */*'
p "chrome, ripetuta (2a)"           $F "$UA_CH" -H 'Accept: */*'
