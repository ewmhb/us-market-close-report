import json, os, sys, urllib.parse, urllib.request

def post(url, data, headers=None):
    request=urllib.request.Request(url,data=urllib.parse.urlencode(data).encode(),headers=headers or {},method="POST")
    with urllib.request.urlopen(request,timeout=30) as response: return json.loads(response.read().decode())

def get(url, headers=None):
    request=urllib.request.Request(url,headers=headers or {},method="GET")
    with urllib.request.urlopen(request,timeout=30) as response: return json.loads(response.read().decode())

def main():
    client_id=os.environ.get("KAKAO_REST_API_KEY"); client_secret=os.environ.get("KAKAO_CLIENT_SECRET")
    refresh_token=os.environ.get("KAKAO_REFRESH_TOKEN"); report_url=os.environ.get("REPORT_URL")
    summary=os.environ.get("REPORT_SUMMARY","미국 증시 장 마감 리포트가 발행되었습니다.")
    if not all((client_id,refresh_token,report_url)): raise RuntimeError("카카오 Secret 또는 리포트 주소가 없습니다.")
    token_data={"grant_type":"refresh_token","client_id":client_id,"refresh_token":refresh_token}
    if client_secret: token_data["client_secret"]=client_secret
    token=post("https://kauth.kakao.com/oauth/token",token_data); access_token=token["access_token"]
    if token.get("refresh_token"): print("::warning::카카오 리프레시 토큰이 갱신되었습니다. Secret을 새 값으로 교체하세요.")
    link={"web_url":report_url,"mobile_web_url":report_url}
    template={"object_type":"text","text":summary[:180],"link":link,"buttons":[{"title":"미국 증시 리포트 보기","link":link}]}
    headers={"Authorization":f"Bearer {access_token}","Content-Type":"application/x-www-form-urlencoded"}
    result=post("https://kapi.kakao.com/v2/api/talk/memo/default/send",{"template_object":json.dumps(template,ensure_ascii=False)},headers)
    if result.get("result_code")!=0: raise RuntimeError(f"나에게 보내기 실패: {result}")
    print("카카오톡 나에게 보내기 완료")
    if os.environ.get("SEND_TO_FRIENDS","false").lower()!="true": return
    friends=get("https://kapi.kakao.com/v1/api/talk/friends?limit=100&order=asc",{"Authorization":f"Bearer {access_token}"}).get("elements",[])
    uuids=[friend["uuid"] for friend in friends if friend.get("uuid")]
    for start in range(0,len(uuids),5):
        batch=uuids[start:start+5]
        sent=post("https://kapi.kakao.com/v1/api/talk/friends/message/default/send",{"receiver_uuids":json.dumps(batch),"template_object":json.dumps(template,ensure_ascii=False)},headers)
        if len(sent.get("successful_receiver_uuids",[]))!=len(batch): raise RuntimeError("일부 친구 발송 실패")
    print(f"카카오톡 친구 {len(uuids)}명에게 보내기 완료")

if __name__=="__main__":
    try: main()
    except Exception as exc: print(str(exc),file=sys.stderr); raise

