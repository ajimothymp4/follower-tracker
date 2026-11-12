import json, os, urllib.parse, urllib.request, urllib.error
T=os.environ["IG_ACCESS_TOKEN"]; U=os.environ["IG_USER_ID"]
def get(fields):
    url=f"https://graph.facebook.com/v26.0/{U}?"+urllib.parse.urlencode({"fields":fields,"access_token":T})
    try:
        with urllib.request.urlopen(url,timeout=30) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        return {"ERROR": json.loads(e.read().decode()).get("error",{}).get("message")}
for h in ["ajkallasvideo","remingtontramsey","tyler_lingle"]:
    for f in ["id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count,view_count,thumbnail_url,media_url",
              "id,media_type,media_product_type,timestamp,permalink,like_count,comments_count,view_count"]:
        d=get(f"business_discovery.username({h}){{media.limit(4){{{f}}}}}")
        if "ERROR" in d: print(h, "fields:", f[:40], "->", d["ERROR"]); continue
        for m in d["business_discovery"]["media"]["data"]:
            m.pop("caption",None)
            for k in ("media_url","thumbnail_url"):
                if k in m: m[k]=m[k][:40]+"..."
            print(h, json.dumps(m))
        break
