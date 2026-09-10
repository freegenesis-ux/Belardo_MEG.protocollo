path = "meg_sources.yaml"
text = open(path, encoding="utf-8").read()

old = '''- id: gnews_wmo_climate_ai
  label: Google News - WMO/Extreme Weather (smistato da AI, sostituisce WMO bloccato)
  url: https://news.google.com/rss/search?q=WMO+OR+%22extreme+weather%22+OR+%22El+Nino%22&hl=en-US&gl=US&ceid=US:en
  type: rss
  tier: 2'''

new = old + '''
- id: gnews_who_disease_ai
  label: Google News - WHO Disease Outbreaks (smistato da AI, sostituisce WHO bloccato)
  url: https://news.google.com/rss/search?q=%22disease+outbreak%22+OR+WHO+OR+epidemic&hl=en-US&gl=US&ceid=US:en
  type: rss
  tier: 2
- id: gnews_imf_econ_ai
  label: Google News - IMF/Crisi Economica (smistato da AI, sostituisce IMF bloccato)
  url: https://news.google.com/rss/search?q=IMF+OR+%22economic+crisis%22+OR+recession&hl=en-US&gl=US&ceid=US:en
  type: rss
  tier: 2'''

assert old in text, "ancora non trovato"
text = text.replace(old, new, 1)
open(path, "w", encoding="utf-8").write(text)
print("2 fonti WHO/IMF aggiunte")
