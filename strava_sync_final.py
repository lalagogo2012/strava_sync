# 1. 필수 라이브러리 설치 (코랩 세션당 최초 1회만 실행하면 됩니다)
# !pip install requests gspread google-auth pandas

import os
import json
import requests
import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
import time
import urllib3

# 코랩 환경 확인 및 드라이브 연결
try:
    from google.colab import drive
    if not os.path.exists('/content/drive'):
        drive.mount('/content/drive', force_remount=True)
    IS_COLAB = True
except ImportError:
    IS_COLAB = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 🔐 사용자 설정 영역 (환경 변수 우선 순위)
# ==========================================
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "178033")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "4e6e4cc232e482dfd9fe2968ef6e38ccd87e0ba0")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN", "db6b58181a6b10e26615da006724a5b2b01b7d77")

# 구글 시트 설정
# 코랩용 파일 경로
COLAB_SERVICE_ACCOUNT = '/content/drive/MyDrive/service_account.json'
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "StravaSync") 

def get_google_creds(scope):
    """환경 변수 또는 파일에서 구글 인증 정보를 가져옵니다."""
    env_creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if env_creds:
        creds_dict = json.loads(env_creds)
        return Credentials.from_service_account_info(creds_dict, scopes=scope)
    
    if os.path.exists(COLAB_SERVICE_ACCOUNT):
        return Credentials.from_service_account_file(COLAB_SERVICE_ACCOUNT, scopes=scope)
    
    raise FileNotFoundError("구글 서비스 계정 인증 정보(JSON)를 찾을 수 없습니다.")

def get_strava_access_token():
    """Refresh Token을 사용하여 유효한 Access Token을 새로 발급받습니다."""
    auth_url = "https://www.strava.com/oauth/token"
    payload = {
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'refresh_token': STRAVA_REFRESH_TOKEN,
        'grant_type': "refresh_token"
    }
    try:
        res = requests.post(auth_url, data=payload, verify=False)
        res.raise_for_status()
        return res.json().get('access_token')
    except Exception as e:
        print(f"❌ 스트라바 토큰 갱신 실패: {e}")
        return None

def format_pace(speed_mps):
    """m/s 속도를 MM:SS 페이스 형식으로 변환"""
    if not speed_mps or speed_mps <= 0: return "00:00"
    pace_seconds = 1000 / speed_mps
    minutes = int(pace_seconds // 60)
    seconds = int(pace_seconds % 60)
    return f"{minutes:02d}:{seconds:02d}"

def get_recent_activities_data(access_token, limit=5):
    """스트라바 API에서 최근 (기본 5개) 활동 목록을 가져와 상세 데이터를 추출합니다."""
    header = {'Authorization': f'Bearer {access_token}'}
    
    # 1. 최근 여러 개의 활동 목록 조회
    activities_url = "https://www.strava.com/api/v3/athlete/activities"
    res = requests.get(activities_url, headers=header, params={'per_page': limit})
    activities = res.json()
    
    if not activities or not isinstance(activities, list):
        print("📍 활동 데이터가 없거나 인증 오류입니다.")
        return []
    
    results = []
    # 2. 가장 과거의 활동부터 처리하여 시트에 시간순으로 쌓이도록 유도 (역순 순회)
    for activity in reversed(activities):
        activity_id = activity['id']
        
        # 상세 데이터 조회
        details_res = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}", headers=header)
        if details_res.status_code != 200:
            print(f"⚠️ 활동 {activity_id} 데이터를 가져오지 못했습니다.")
            continue
            
        details = details_res.json()
        print(f"🚀 [분석] '{details.get('name')}'의 데이터를 수집 중...")
        
        summary = {
            "구분": "전체 요약",
            "날짜": details.get('start_date_local', '').replace('T', ' ').replace('Z', ''),
            "거리(km)": round(details.get('distance', 0) / 1000, 2),
            "시간": str(pd.to_timedelta(details.get('moving_time', 0), unit='s')).split()[-1],
            "평균페이스": format_pace(details.get('average_speed')),
            "평균GAP": format_pace(details.get('average_temp')), 
            "평균심박": int(details.get('average_heartrate', 0)),
            "최대심박": int(details.get('max_heartrate', 0)),
            "평균케이던스": int(details.get('average_cadence', 0) * 2) if details.get('average_cadence') else 0,
            "평균파워(W)": int(details.get('average_watts', 0)),
            "획득고도(m)": int(details.get('total_elevation_gain', 0)),
            "상대적노력": details.get('suffer_score', 0),
            "칼로리": int(details.get('calories', 0)),
            "activityId": str(activity_id)
        }

        if 'splits_metric' in details:
            valid_gaps = [s.get('average_grade_adjusted_speed', 0) for s in details['splits_metric'] if s.get('average_grade_adjusted_speed')]
            if valid_gaps:
                avg_gap_speed = sum(valid_gaps) / len(valid_gaps)
                summary["평균GAP"] = format_pace(avg_gap_speed)
        
        lap_rows = []
        laps = details.get('laps', [])
        for i, lap in enumerate(laps, 1):
            lap_row = {
                "구분": f"Lap {i}",
                "날짜": "-",
                "거리(km)": round(lap.get('distance', 0) / 1000, 2),
                "시간": str(pd.to_timedelta(lap.get('moving_time', 0), unit='s')).split()[-1],
                "평균페이스": format_pace(lap.get('average_speed')),
                "평균GAP": "-", 
                "평균심박": int(lap.get('average_heartrate', 0)),
                "최대심박": int(lap.get('max_heartrate', 0)),
                "평균케이던스": int(lap.get('average_cadence', 0) * 2) if lap.get('average_cadence') else 0,
                "평균파워(W)": int(lap.get('average_watts', 0)),
                "획득고도(m)": int(lap.get('total_elevation_gain', 0)),
                "상대적노력": "-", "칼로리": "-", "activityId": str(activity_id)
            }
            lap_rows.append(lap_row)
            
        results.append({"summary": summary, "laps": lap_rows})
            
    return results

def sync_multiple_to_sheets(data_list):
    """구글 시트에 여러 데이터를 한 번에 기록합니다."""
    if not data_list:
        return
        
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    try:
        creds = get_google_creds(scope)
        gc = gspread.authorize(creds)
        sh = gc.open(SPREADSHEET_NAME)
        worksheet = sh.get_worksheet(0)
        
        # 첫 번째 항목 기준으로 헤더 가져오기
        headers = list(data_list[0]["summary"].keys())
        all_rows = worksheet.get_all_values()
        
        if not all_rows or not all_rows[0] or all_rows[0][0] != "구분":
            print("📝 [시트] 제목 행 설정...")
            worksheet.clear()
            worksheet.update(values=[headers], range_name='A1')
            time.sleep(1.5)
            all_rows = worksheet.get_all_values()

        # 시트에 이미 입력된 가민 로그의 activityId 판별 (중복 방지 용도)
        if len(all_rows) > 1:
            existing_ids = set(row[-1] for row in all_rows if len(row) > 0 and row[0] == "전체 요약")
        else:
            existing_ids = set()

        final_rows = []
        new_activity_count = 0
        
        # 저장할 데이터 묶 만들기
        for data in data_list:
            activity_id = data["summary"]["activityId"]
            if activity_id in existing_ids:
                print(f"✔️ 이미 동기화된 활동입니다. ({data['summary']['날짜']})")
                continue
                
            print(f"✅ 동기화 목록에 추가! ({data['summary']['날짜']})")
            final_rows.append(list(data["summary"].values()))
            for lap in data["laps"]:
                final_rows.append(list(lap.values()))
            new_activity_count += 1
                
        # 리스트에 모인 데이터가 있으면 일괄 저장
        if final_rows:
            worksheet.append_rows(final_rows)
            print(f"🎉 총 {new_activity_count}개의 새로운 활동 시트에 기록 완료!")
        else:
            print("💡 새로 동기화할 활동이 없습니다.")
        
    except Exception as e:
        print(f"❌ 오류: {e}")

if __name__ == "__main__":
    print("🏁 스트라바 데이터 자동 동기화 엔진 가동")
    token = get_strava_access_token()
    if token:
        # 최근 5개의 로그를 가져옴 (필요시 숫자를 늘리면 됩니다)
        activity_data_list = get_recent_activities_data(token, limit=5)
        if activity_data_list:
            sync_multiple_to_sheets(activity_data_list)
