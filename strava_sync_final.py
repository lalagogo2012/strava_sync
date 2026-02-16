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
# 깃허브 액션(Actions)에서는 Secrets에 저장된 값을 가져오고, 
# 코랩에서는 아래 기본값을 사용합니다.
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "178033")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "4e6e4cc232e482dfd9fe2968ef6e38ccd87e0ba0")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN", "db6b58181a6b10e26615da006724a5b2b01b7d77")

# 구글 시트 설정
# 코랩용 파일 경로
COLAB_SERVICE_ACCOUNT = '/content/drive/MyDrive/service_account.json'
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "StravaSync") 

def get_google_creds(scope):
    """환경 변수 또는 파일에서 구글 인증 정보를 가져옵니다."""
    # 깃허브 액션 환경 (Secrets에 JSON 문자열 저장 시)
    env_creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if env_creds:
        creds_dict = json.loads(env_creds)
        return Credentials.from_service_account_info(creds_dict, scopes=scope)
    
    # 코랩 또는 로컬 파일 환경
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

def get_latest_activity_details(access_token):
    """스트라바 API의 모든 가용 지표를 추출합니다."""
    header = {'Authorization': f'Bearer {access_token}'}
    
    # 1. 최근 활동 목록 조회
    activities_url = "https://www.strava.com/api/v3/athlete/activities"
    res = requests.get(activities_url, headers=header, params={'per_page': 1})
    activities = res.json()
    
    if not activities or not isinstance(activities, list):
        print("📍 활동 데이터가 없거나 인증 오류입니다.")
        return None
        
    activity_id = activities[0]['id']
    # 2. 상세 데이터 조회
    details = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}", headers=header).json()
    
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
        
    return {"summary": summary, "laps": lap_rows}

def sync_to_sheets(data):
    """구글 시트에 데이터를 기록합니다."""
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    try:
        creds = get_google_creds(scope)
        gc = gspread.authorize(creds)
        sh = gc.open(SPREADSHEET_NAME)
        worksheet = sh.get_worksheet(0)
        all_rows = worksheet.get_all_values()
        headers = list(data["summary"].keys())
        
        if not all_rows or not all_rows[0] or all_rows[0][0] != "구분":
            print("📝 [시트] 제목 행 설정...")
            worksheet.clear()
            worksheet.update(values=[headers], range_name='A1')
            time.sleep(1.5)
            all_rows = worksheet.get_all_values()

        if len(all_rows) > 1:
            existing_ids = [row[-1] for row in all_rows if len(row) > 0 and row[0] == "전체 요약"]
        else:
            existing_ids = []

        if data["summary"]["activityId"] in existing_ids:
            print(f"✔️ 이미 동기화된 활동입니다. ({data['summary']['날짜']})")
            return

        final_rows = [list(data["summary"].values())] + [list(lap.values()) for lap in data["laps"]]
        worksheet.append_rows(final_rows)
        print(f"✅ 동기화 완료! ({data['summary']['날짜']})")
        
    except Exception as e:
        print(f"❌ 오류: {e}")

if __name__ == "__main__":
    print("🏁 스트라바 데이터 자동 동기화 엔진 가동")
    token = get_strava_access_token()
    if token:
        activity_data = get_latest_activity_details(token)
        if activity_data:
            sync_to_sheets(activity_data)