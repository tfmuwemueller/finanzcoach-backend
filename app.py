# -*- coding: utf-8 -*-

import yfinance as yf
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
import openai
from sklearn.preprocessing import MinMaxScaler
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import Dense, LSTM, Input
from flask import Flask, jsonify
from pyngrok import ngrok
from fredapi import Fred
import logging
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
from alpha_vantage.timeseries import TimeSeries
from alpha_vantage.fundamentaldata import FundamentalData
from fredapi import Fred
import praw
import tensorflow as tf

# Sichere Nutzung der API-Keys über Environment Variables
openai.api_key = os.environ.get('OPENAI_API_KEY')
ALPHA_API_KEY = os.environ.get('ALPHA_API_KEY')
FRED_API_KEY = os.environ.get('FRED_API_KEY')
FINNHUB_API_KEY = os.environ.get('FINNHUB_API_KEY')
API_KEY = os.getenv('API_KEY')

if API_KEY is None:
    raise ValueError("API_KEY ist nicht gesetzt! Bitte Umgebungsvariable prüfen.")

fred = Fred(api_key=FRED_API_KEY)

reddit = praw.Reddit(
    client_id=os.environ.get('REDDIT_CLIENT_ID'),
    client_secret=os.environ.get('REDDIT_SECRET'),
    user_agent=os.environ.get('REDDIT_USER_AGENT')
)

logging.basicConfig(
    filename='analyse_logs.log',
    level=logging.ERROR,
    format='%(asctime)s:%(levelname)s:%(message)s'
)

from pycoingecko import CoinGeckoAPI
cg = CoinGeckoAPI()

def get_crypto_data(ticker, days=365):  # Maximal 1 Jahr Datenhistorie erlaubt
    coin_data = cg.get_coin_market_chart_by_id(id=ticker, vs_currency='usd', days=days)
    prices = [price[1] for price in coin_data['prices']]
    return prices

from alpha_vantage.timeseries import TimeSeries
import pandas as pd

def get_alpha_vantage_data(ticker):
    ts = TimeSeries(key=API_KEY, output_format='pandas')
    data, meta_data = ts.get_daily(symbol=ticker, outputsize='full')
    return data

from alpha_vantage.fundamentaldata import FundamentalData

def get_alpha_vantage_dividend(ticker):
    fd = FundamentalData(key=API_KEY, output_format='json')
    try:
        overview, _ = fd.get_company_overview(symbol=ticker)
        dividend_yield = overview.get("DividendYield")
        if dividend_yield:
            return float(dividend_yield) * 100  # Prozent
        else:
            return "N/A"
    except Exception as e:
        return f"Fehler: {e}"

def get_stock_data(ticker, period="5y"):
    df = yf.download(ticker, period=period, auto_adjust=True)
    if df.empty:
        print("⚠️ Keine Daten gefunden!")
        return df

    # Technische Indikatoren
    df['MA50'] = df['Close'].rolling(window=50).mean()
    df['MA100'] = df['Close'].rolling(window=100).mean()
    df['MA200'] = df['Close'].rolling(window=200).mean()

    # RSI Berechnung
    delta = df['Close'].diff(1)
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=13, adjust=False).mean()
    ema_down = down.ewm(com=13, adjust=False).mean()
    rs = ema_up / ema_down
    df['RSI'] = 100 - (100 / (1 + rs))

    df.dropna(inplace=True)
    return df

def get_etf_data(ticker, period='3y'):
    df = yf.download(ticker, period=period, auto_adjust=True)
    df.reset_index(inplace=True)
    return df

import requests

def get_dividend_finnhub(ticker):
    url = f'https://finnhub.io/api/v1/stock/metric?symbol={ticker}&metric=all&token={FINNHUB_API_KEY}'

    response = requests.get(url)
    data = response.json()

    try:
        dividend_yield = data['metric']['dividendYieldIndicatedAnnual']
        if dividend_yield is not None:
            return float(dividend_yield)
        else:
            return "N/A"
    except (KeyError, TypeError):
        return "N/A"

from alpha_vantage.timeseries import TimeSeries

def get_bond_data(symbol, outputsize='full'):
    ts = TimeSeries(key=ALPHA_API_KEY, output_format='pandas')
    data, _ = ts.get_daily(symbol=symbol, outputsize=outputsize)
    data.reset_index(inplace=True)
    data.rename(columns={
        'date': 'Date',
        '1. open': 'Open',
        '2. high': 'High',
        '3. low': 'Low',
        '4. close': 'Close',
        '5. volume': 'Volume'
    }, inplace=True)
    return data

import functools
import logging
import time
import yfinance as yf

@functools.lru_cache(maxsize=100)
def get_fundamentals(ticker, full_name):
    stock = yf.Ticker(ticker)

    info = {}
    try:
        info = stock.info
    except yf.YFRateLimitError:
        logging.warning(f"Rate Limit erreicht für {ticker}, warte 10 Sekunden und versuche erneut...")
        time.sleep(10)
        try:
            info = stock.info
        except Exception as e:
            logging.error(f"Erneuter Fehler bei Yahoo-Abfrage für {ticker}: {str(e)}")
            info = {}
    except Exception as e:
        logging.error(f"Allgemeiner Fehler bei Yahoo-Abfrage für {ticker}: {str(e)}")
        info = {}

    yahoo_dividend = info.get("dividendYield", 0) * 100 if info.get("dividendYield") else "N/A"

    try:
        alpha_dividend = get_alpha_vantage_dividend(ticker)
    except Exception as e:
        logging.error(f"Fehler Alpha Vantage Dividend {ticker}: {str(e)}")
        alpha_dividend = "N/A"

    try:
        finnhub_dividend = get_dividend_finnhub(ticker)
    except Exception as e:
        logging.error(f"Fehler Finnhub Dividend {ticker}: {str(e)}")
        finnhub_dividend = "N/A"

    # GPT-Validierung
    try:
        dividend_validation = validate_dividend_extended(yahoo_dividend, alpha_dividend, finnhub_dividend)
    except Exception as e:
        logging.error(f"Fehler bei GPT-Dividendenvalidierung {ticker}: {str(e)}")
        dividend_validation = f"GPT-Validierung Fehler: {str(e)}"

    fundamentals = {
        "KGV": info.get("trailingPE", "N/A"),
        "Dividendenrendite (%) Yahoo": yahoo_dividend,
        "Dividendenrendite (%) Alpha": alpha_dividend,
        "Dividendenrendite (%) Finnhub": finnhub_dividend,
        "Dividenden-Validierung (GPT)": dividend_validation,
        "Marktkapitalisierung (Mrd.)": info.get("marketCap", 0) / 1e9 if info.get("marketCap") else "N/A",
        "Branche (vorläufig)": info.get("industry", "N/A"),
        "ESG-Score": info.get("esgScore", "N/A")
    }

    # GPT-Klassifizierung der Branche
    try:
        gpt_response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Klassifiziere präzise Branche und ESG-Relevanz."},
                {"role": "user", "content": f"Unternehmen: {full_name}, Branche: {fundamentals['Branche (vorläufig)']}"}
            ]
        )
        fundamentals["Branche (GPT)"] = gpt_response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Fehler GPT Branchen-Klassifizierung {ticker}: {str(e)}")
        fundamentals["Branche (GPT)"] = f"GPT-Fehler: {str(e)}"

    return fundamentals

def validate_dividend_extended(yahoo_div, alpha_div, finnhub_div):
    yahoo_div = yahoo_div if isinstance(yahoo_div, (int, float)) else "N/A"
    alpha_div = alpha_div if isinstance(alpha_div, (int, float)) else "N/A"
    finnhub_div = finnhub_div if isinstance(finnhub_div, (int, float)) else "N/A"

    prompt = f"""
    Prüfe folgende Dividendenrenditen:
    - Yahoo Finance: {yahoo_div if yahoo_div != 'N/A' else 'unrealistisch'}%
    - Alpha Vantage: {alpha_div if alpha_div != 'N/A' else 'unrealistisch'}%
    - Finnhub: {finnhub_div if finnhub_div != 'N/A' else 'unrealistisch'}%

    Entscheide, welche Dividendenrendite realistisch ist, und gib prägnant an:
    "Yahoo", "Alpha Vantage", "Finnhub" oder "Keiner", falls alle unrealistisch sind.
    Liefere kurz eine Begründung dazu.
    """

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Du validierst Dividenden gewissenhaft und präzise."},
            {"role": "user", "content": prompt}
        ]
    )

    validated_response = response.choices[0].message.content.strip()

    if "Keiner" in validated_response or all(div == "N/A" or (isinstance(div, (int, float)) and div > 20.0)
                                             for div in [yahoo_div, alpha_div, finnhub_div]):
        validated_response += "\n\n⚠️ Hinweis: Keine zuverlässigen Dividendeninformationen verfügbar. Bitte manuell prüfen!"

    return validated_response

def reduce_etf_data(raw_data):
    # Beispielhafte Reduktion der API-Daten auf wesentliche Kerninformationen
    return {
        "name": raw_data.get("Name"),
        "isin": raw_data.get("ISIN"),
        "kurzbeschreibung": raw_data.get("Description", "")[:500],
        "performance": raw_data.get("PerformanceYTD"),
        "esg_rating": raw_data.get("ESGRating"),
        "top_holdings": raw_data.get("TopHoldings", [])[:10],
        "branche": raw_data.get("SectorExposure", [])
    }

def analyse_sentiment(ticker, full_name):
    try:
        sources = ['Yahoo Finance', 'MarketWatch', 'Google News', 'Reuters', 'Finviz', 'Social Media']
        sentiment_results = {}

        for source in sources:
            # hier sollte der echte API-Call stehen
            sentiment_results[source] = "neutral"

        # GPT-Validierung
        final_sentiment = validate_sentiment_gpt(sentiment_results)

        return {"Final (GPT-Validiert)": final_sentiment, **sentiment_results}

    except Exception as e:
        logging.error(f"Fehler bei Sentimentanalyse {ticker}: {str(e)}")
        return {
            'Final (GPT-Validiert)': 'nicht verfügbar',
            'Fehler': f"Sentimentdaten nicht verfügbar: {str(e)}"
        }

import openai

def validate_sentiment_gpt(sentiments):
    prompt = f"""
    Du erhältst folgende Sentiment-Daten aus verschiedenen Quellen:
    {sentiments}

    Analysiere diese kurz und entscheide dich für eine finale Sentiment-Einschätzung:
    positiv, negativ oder neutral.

    Gib nur das finale Sentiment zurück.
    """

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Du bist ein Sentiment-Analyse-Experte."},
            {"role": "user", "content": prompt}
        ]
    )

    validated_sentiment = response.choices[0].message.content.strip().lower()

    # Sicherstellen, dass die Antwort immer gültig ist
    if validated_sentiment not in ["positiv", "negativ", "neutral"]:
        validated_sentiment = "neutral"

    return validated_sentiment

def predict_stock_price(df, days_to_predict=30, prediction_days=60, epochs=50):
    try:
        data = df['Close'].values.reshape(-1, 1)
        scaler = MinMaxScaler()
        scaled_data = scaler.fit_transform(data)

        x_train, y_train = [], []

        for i in range(prediction_days, len(scaled_data)):
            x_train.append(scaled_data[i-prediction_days:i, 0])
            y_train.append(scaled_data[i, 0])

        x_train, y_train = np.array(x_train), np.array(y_train)
        x_train = np.reshape(x_train, (x_train.shape[0], x_train.shape[1], 1))

        model = Sequential()
        model.add(Input(shape=(x_train.shape[1], 1)))
        model.add(LSTM(units=50, activation='relu'))
        model.add(Dense(1))
        model.compile(optimizer='adam', loss='mean_squared_error')

        model.fit(x_train, y_train, epochs=epochs, batch_size=32, verbose=0)

        # Prognose erstellen
        last_days = scaled_data[-prediction_days:]
        prediction_list = []

        for _ in range(days_to_predict):
            pred_input = last_days[-prediction_days:].reshape(1, prediction_days, 1)
            pred = model.predict(pred_input, verbose=0)[0, 0]
            prediction_list.append(pred)
            last_days = np.append(last_days, pred)

        predicted_prices = scaler.inverse_transform(np.array(prediction_list).reshape(-1, 1))
        return predicted_prices.flatten().tolist()

    except Exception as e:
        logging.error(f"Fehler bei Prognoseberechnung: {str(e)}")
        return ["Prognosedaten nicht verfügbar"]

def predict_crypto_price(prices, days_to_predict=30, prediction_days=60, epochs=50):
    data = np.array(prices).reshape(-1, 1)
    scaler = MinMaxScaler()
    scaled_data = scaler.fit_transform(data)

    x_train, y_train = [], []

    for i in range(prediction_days, len(scaled_data)):
        x_train.append(scaled_data[i-prediction_days:i, 0])
        y_train.append(scaled_data[i, 0])

    x_train, y_train = np.array(x_train), np.array(y_train)
    x_train = np.reshape(x_train, (x_train.shape[0], x_train.shape[1], 1))

    model = Sequential()
    model.add(Input(shape=(x_train.shape[1], 1)))
    model.add(LSTM(units=50, activation='relu'))
    model.add(Dense(1))
    model.compile(optimizer='adam', loss='mean_squared_error')

    model.fit(x_train, y_train, epochs=epochs, batch_size=32, verbose=0)

    last_days = scaled_data[-prediction_days:]
    prediction_list = []

    for _ in range(days_to_predict):
        pred_input = last_days[-prediction_days:].reshape(1, prediction_days, 1)
        pred = model.predict(pred_input, verbose=0)[0, 0]
        prediction_list.append(pred)
        last_days = np.append(last_days, pred)

    predicted_prices = scaler.inverse_transform(np.array(prediction_list).reshape(-1, 1))
    return predicted_prices.flatten().tolist()

from alpha_vantage.fundamentaldata import FundamentalData
import openai
import logging

fd = FundamentalData(key=API_KEY, output_format='json')

def get_rating_alpha_vantage(ticker):
    try:
        data, _ = fd.get_company_overview(symbol=ticker)
        rating = data.get('CreditRating', 'N/A')
        return rating
    except Exception as e:
        logging.error(f"Fehler Alpha Vantage Rating {ticker}: {str(e)}")
        return "N/A"

def gpt_rating_fallback(entity):
    prompt = f"Wie lautet das aktuelle Kreditrating (S&P, Moody’s, Fitch) von {entity}? Gib nur die Rating-Stufen an (z.B. AA+, Baa1, BBB)."

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Nenne nur die aktuelle Rating-Stufe ohne weitere Erklärungen."},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content.strip()

from alpha_vantage.timeseries import TimeSeries
import pandas as pd

def get_commodity_data(symbol, interval='daily'):
    ts = TimeSeries(key=API_KEY, output_format='pandas')
    data, meta_data = ts.get_daily(symbol=symbol, outputsize='compact')
    data.rename(columns={
        '1. open': 'Open',
        '2. high': 'High',
        '3. low': 'Low',
        '4. close': 'Close',
        '5. volume': 'Volume'
    }, inplace=True)
    return data

# Symbole für Alpha Vantage Rohstoffe:
symbols = {
    "Gold": "XAUUSD",
    "Silber": "XAGUSD",
    "Kupfer": "HGUSD",
    "Brent Öl": "BNO",
    "WTI Öl": "WTI",
    "Erdgas": "NG"
}

import openai

def get_commodity_sentiment(rohstoff, preis_trend):
    prompt = f"""
    Der Rohstoff ist {rohstoff}. Der aktuelle Preistrend und Marktstatus ist:
    {preis_trend}

    Fasse die aktuelle Marktentwicklung kurz zusammen und erläutere wichtige Einflüsse auf den Markt in maximal 2-3 Sätzen.
    """

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Du bist ein Rohstoffmarkt-Analyst."},
            {"role": "user", "content": prompt}
        ]
    )

    return response.choices[0].message.content.strip()

import praw
import openai

# Reddit API Credentials

def get_reddit_sentiment(subreddit_name, keyword, num_posts=100):
    subreddit = reddit.subreddit(subreddit_name)
    posts = subreddit.search(keyword, limit=num_posts)

    texts = [post.title + " " + post.selftext for post in posts]

    prompt = f"Analysiere das allgemeine Sentiment aus diesen Reddit-Posts zu {keyword}: {texts}"

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": "Analysiere präzise das Sentiment (positiv, neutral, negativ)."},
                  {"role": "user", "content": prompt}]
    )

    sentiment = response.choices[0].message.content.strip()
    return sentiment

from flask import Flask, jsonify
from pyngrok import ngrok
import logging
from fredapi import Fred
import numpy as np
import requests
from bs4 import BeautifulSoup
import openai
import praw
import tweepy

app = Flask(__name__)

# --- Analyse-Endpunkte ---

@app.route('/analyse/<asset_type>/<ticker>/<full_name>')
def analyse(asset_type, ticker, full_name):
    try:
        fundamentals = {}
        prognose = []

        if asset_type.lower() == 'crypto':
            prices = get_crypto_data(ticker)
            prognose = predict_crypto_price(prices)

        elif asset_type.lower() == 'etf':
            data = get_etf_data(ticker)
            prognose = predict_stock_price(data)

        elif asset_type.lower() == 'bond':
            data = get_bond_data(ticker)
            prognose = predict_stock_price(data)

        else:  # Aktien (Standardfall)
            data = get_stock_data(ticker)
            fundamentals = get_fundamentals(ticker, full_name)
            prognose = predict_stock_price(data)

        sentiment = analyse_sentiment(ticker, full_name)

        return jsonify({
            "fundamentals": fundamentals,
            "sentiment": sentiment,
            "prognose": prognose
        })

    except Exception as e:
        logging.error(f"Allgemeiner Fehler bei Analyse für {ticker}: {str(e)}")
        return jsonify({"Fehler": f"Analyse fehlgeschlagen: {str(e)}"}), 500


# --- OECD Inflation Integration ---

OECD_COUNTRY_CODES = {
    "frankreich": "FRA",
    "italien": "ITA",
    "kanada": "CAN",
    "suedkorea": "KOR",
    "australien": "AUS",
    "neuseeland": "NZL"
}

INFLATION_CONFIG_FRED = {
    "usa": ("CPIAUCSL", "USA"),
    "eurozone": ("CP0000EZ19M086NEST", "Eurozone (aggregiert)"),
    "deutschland": ("DEUCPIALLMINMEI", "Deutschland"),
    "gb": ("GBRCPIALLMINMEI", "Vereinigtes Königreich"),
    "japan": ("JPNCPIALLMINMEI", "Japan"),
    "china": ("CHNCPIALLMINMEI", "China"),
    "brasilien": ("BRACPIALLMINMEI", "Brasilien"),
}

def get_oecd_inflation(country_code):
    headers = {'Accept': 'application/json'}
    url = f"https://stats.oecd.org/SDMX-JSON/data/PRICES_CPI/{country_code}.CPALTT01.GY.M/all?lastNObservations=1"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    try:
        observations = data['dataSets'][0]['series']['0:0:0:0']['observations']
        latest_key = max(observations.keys(), key=int)
        return observations[latest_key][0]
    except (KeyError, IndexError) as e:
        raise ValueError(f"OECD Datenstruktur unerwartet: {e}")

# GPT-Fallback bei OECD Fehler
def gpt_inflation_fallback(region):
    prompt = f"Wie hoch ist aktuell die Inflationsrate in {region.capitalize()}? Bitte nenne nur die Zahl in Prozent."
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Gib nur die Inflationsrate in Prozent zurück."},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content.strip()

@app.route('/makro/inflation/<region>')
def inflation(region):
    region = region.lower()

    if region in INFLATION_CONFIG_FRED:
        series_id, display_name = INFLATION_CONFIG_FRED[region]
        try:
            data = fred.get_series(series_id).dropna()
            if len(data) < 13:
                raise ValueError("Nicht genügend Datenpunkte zur Berechnung")
            inflation_rate = ((data.iloc[-1] - data.iloc[-13]) / data.iloc[-13]) * 100

            return jsonify({
                "Land": display_name,
                "Inflationsrate (%)": round(inflation_rate, 2)
            })

        except Exception as e:
            logging.error(f"FRED Fehler ({region}): {str(e)}")
            return jsonify({"Fehler": f"FRED Datenfehler: {str(e)}"}), 500

    elif region in OECD_COUNTRY_CODES:
        try:
            inflation_rate = get_oecd_inflation(OECD_COUNTRY_CODES[region])
        except Exception as e:
            logging.error(f"OECD Fehler ({region}): {str(e)}")
            inflation_rate = gpt_inflation_fallback(region)

        return jsonify({
            "Land": region.capitalize(),
            "Inflationsrate (%)": inflation_rate
        })

    else:
        return jsonify({"Fehler": "Region nicht unterstützt oder Daten unzureichend"}), 400

# Leitzinsimplementierung

LEITZINS_CONFIG_FRED = {
    "usa": ("FEDFUNDS", "USA"),
    "eurozone": ("ECBMRRFR", "Eurozone"),
    "gb": ("BOERUKM", "Großbritannien"),
    "japan": ("IRSTCI01JPM156N", "Japan")
}

@app.route('/makro/leitzins/<region>')
def leitzins(region):
    region = region.lower()

    if region in LEITZINS_CONFIG_FRED:
        series_id, display_name = LEITZINS_CONFIG_FRED[region]
        try:
            data = fred.get_series(series_id).dropna()
            leitzins = data.iloc[-1]

            return jsonify({
                "Land": display_name,
                "Leitzins (%)": round(leitzins, 2)
            })

        except Exception as e:
            logging.error(f"FRED Fehler (Leitzins {region}): {str(e)}")
            # Fallback via GPT, falls FRED scheitert
            leitzins = gpt_leitzins_fallback(region)

    else:
        # Region nicht in FRED, direkte GPT-Abfrage
        leitzins = gpt_leitzins_fallback(region)

    return jsonify({
        "Land": region.capitalize(),
        "Leitzins (%)": leitzins
    })

def gpt_leitzins_fallback(region):
    prompt = f"Wie hoch ist aktuell der Leitzins in {region.capitalize()}? Bitte gib ausschließlich die Zahl in Prozent an."
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Gib nur den aktuellen Leitzins in Prozent an, ohne weitere Erklärungen."},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content.strip()

# Politische Statements

@app.route('/politik/<land>/<person>', methods=['GET'])
def politisches_sentiment(land, person):
    try:
        prompt = f"Bewerte kurz, ob die aktuellsten Aussagen von {person.capitalize()} in {land.capitalize()} eher positive, negative oder neutrale Auswirkungen auf die Finanzmärkte haben. Gib nur an: 'positiv', 'negativ' oder 'neutral', plus eine kurze Begründung."

        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Bewerte präzise und knapp den Einfluss politischer Aussagen auf Finanzmärkte."},
                {"role": "user", "content": prompt}
            ]
        )

        sentiment = response.choices[0].message.content.strip()

        return jsonify({
            "Person": person.capitalize(),
            "Land": land.capitalize(),
            "Sentiment": sentiment
        })

    except Exception as e:
        logging.error(f"Fehler bei politischem Sentiment für {person} in {land}: {str(e)}")
        return jsonify({"Fehler": f"Sentiment nicht verfügbar: {str(e)}"}), 500

# Handelskonflikte

@app.route('/handel/<land1>/<land2>', methods=['GET'])
def handelskonflikte(land1, land2):
    try:
        prompt = f"Bewerte kurz die aktuellen Handelsbeziehungen zwischen {land1.capitalize()} und {land2.capitalize()}. Gib an, ob diese Handelskonflikte oder Zollmaßnahmen eher positiv, negativ oder neutral auf die globalen Märkte wirken könnten. Antworte mit 'positiv', 'negativ' oder 'neutral' sowie einer kurzen Erklärung."

        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Du bewertest Handelskonflikte und Zollmaßnahmen präzise und knapp hinsichtlich ihrer Auswirkungen auf globale Finanzmärkte."},
                {"role": "user", "content": prompt}
            ]
        )

        handels_sentiment = response.choices[0].message.content.strip()

        return jsonify({
            "Land1": land1.capitalize(),
            "Land2": land2.capitalize(),
            "Handelskonflikt-Sentiment": handels_sentiment
        })

    except Exception as e:
        logging.error(f"Fehler bei Handelskonflikt-Abfrage {land1}-{land2}: {str(e)}")
        return jsonify({"Fehler": f"Sentiment nicht verfügbar: {str(e)}"}), 500

# Rohstoffpreise und Lieferketten

@app.route('/rohstoffe/sentiment/<rohstoff>')
def rohstoff_sentiment(rohstoff):
    try:  # <-- hier fehlte try
        prompt = f"Wie ist aktuell der Markt für {rohstoff.capitalize()} einzuschätzen? Antworte mit 'steigend', 'fallend' oder 'stabil' plus kurze Begründung."
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "Analysiere präzise den Rohstoffmarkt."},
                      {"role": "user", "content": prompt}]
        )
        markt_sentiment = response.choices[0].message.content.strip()
        return jsonify({
            "Rohstoff": rohstoff.capitalize(),
            "Marktentwicklung": markt_sentiment
        })

    except Exception as e:
        logging.error(f"Fehler bei Rohstoffabfrage {rohstoff}: {str(e)}")
        return jsonify({"Fehler": f"Rohstoffdaten nicht verfügbar: {str(e)}"}), 500

# Rohtstoffpreisaktualität

@app.route('/rohstoffe/preis/<commodity>')
def get_rohstoff(commodity):
    symbols = {
        "gold": "XAUUSD",
        "silber": "XAGUSD",
        "kupfer": "HGUSD",
        "brent": "BNO",
        "wti": "WTI",
        "erdgas": "NG"
    }
    symbol = symbols.get(commodity.lower())
    if symbol:
        data = get_commodity_data(symbol)
        latest = data.iloc[-1].to_dict()

        preis_trend = f"Preis: {latest['Close']} USD, Tagesveränderung: {latest['Close'] - latest['Open']} USD"
        markt_sentiment = get_commodity_sentiment(commodity, preis_trend)

        return jsonify({
            "Rohstoff": commodity.capitalize(),
            "Preisinfo": latest,
            "Marktentwicklung": markt_sentiment
        })
    else:
        return jsonify({"Fehler": "Rohstoff nicht gefunden"}), 404

# Insider-Trading-Aktivitaeten

@app.route('/insider/<ticker>', methods=['GET'])
def insider_trading(ticker):
    try:
        url = f'https://finnhub.io/api/v1/stock/insider-transactions?symbol={ticker}&token={FINNHUB_API_KEY}'
        response = requests.get(url)
        data = response.json()

        recent_trades = data.get('data', [])[:5]

        summary = "\n".join([
            f"{trade.get('name', 'Unbekannt')} - {trade.get('transactionType', 'N/A')} - "
            f"{trade.get('share', 'N/A')} shares am {trade.get('transactionDate', 'N/A')}"
            for trade in recent_trades
        ])

        gpt_response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Analysiere kurz Insider-Trades hinsichtlich Markt-Signal."},
                {"role": "user", "content": f"Insider-Trades für {ticker}:\n{summary}\n\nSind diese Trades eher ein positives, negatives oder neutrales Signal?"}
            ]
        )

        insider_sentiment = gpt_response.choices[0].message.content.strip()

        return jsonify({
            "Ticker": ticker,
            "Insider-Trades": recent_trades,
            "Markt-Signal": insider_sentiment
        })

    except Exception as e:
        logging.error(f"Fehler Insider Trading {ticker}: {str(e)}")
        return jsonify({"Fehler": f"Insiderdaten nicht verfügbar: {str(e)}"}), 500

# Rating-Agenturen

@app.route('/rating/<ticker>', methods=['GET'])
def rating(ticker):
    rating_av = get_rating_alpha_vantage(ticker)
    if rating_av == "N/A":
        rating_av = gpt_rating_fallback(ticker)

    return jsonify({
        "Ticker": ticker,
        "Rating": rating_av
    })

# Reddit Anbindung
import praw

@app.route('/sentiment/reddit/<string:subreddit_name>/<string:keyword>')
def reddit_sentiment(subreddit_name, keyword):
    try:
        subreddit = reddit.subreddit(subreddit_name)
        posts = subreddit.search(keyword, limit=50)

        texts = [f"{post.title} {post.selftext}" for post in posts]

        if not texts:
            return jsonify({
                "subreddit": subreddit_name,
                "keyword": keyword,
                "sentiment": "Keine relevanten Beiträge gefunden.",
                "beispiele": []
            })

        # GPT-Sentimentanalyse
        prompt = f"""
        Analysiere das allgemeine Sentiment (positiv, neutral, negativ) der folgenden Reddit-Beiträge zum Thema "{keyword}":
        {texts}

        Gib prägnant zurück: "positiv", "neutral" oder "negativ" plus kurze Begründung.
        """

        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Analysiere das Sentiment der Reddit-Beiträge präzise und knapp."},
                {"role": "user", "content": prompt}
            ]
        )

        sentiment = response.choices[0].message.content.strip()

        return jsonify({
            "subreddit": subreddit_name,
            "keyword": keyword,
            "sentiment": sentiment,
            "beispiele": texts[:5]  # Die ersten 5 Beiträge als Beispiele
        })

    except Exception as e:
        logging.error(f"Fehler bei Reddit-Sentiment für {subreddit_name}/{keyword}: {str(e)}")
        return jsonify({"Fehler": f"Sentimentanalyse fehlgeschlagen: {str(e)}"}), 500

# Twitter Anbindung - geht nicht wg free account

@app.route('/twitter/test')
def twitter_test():
    import tweepy

    consumer_key="YWI2w1jZPre0geoaRw2bJK6xN"
    consumer_secret="p2QwLzLBVFiXkyOrhHpvWc3WqgZgk95lmA7DndkCnX2HvOeABi"
    access_token="1919114015588151296-sVoAim99ff3wg2t7u0AnWBAitjGNeS"
    access_token_secret="gAmwGxwdDyS75enwiniqZPwVoGjj7val6S5LJ3lpTsYNJ"

    auth = tweepy.OAuth1UserHandler(
        consumer_key, consumer_secret,
        access_token, access_token_secret
    )
    api = tweepy.API(auth)

    try:
        tweets = api.home_timeline(count=1)
        if tweets:
            return jsonify({"status": "✅ Twitter API funktioniert!", "tweet": tweets[0].text})
        else:
            return jsonify({"status": "⚠️ Twitter API erreichbar, aber keine Tweets gefunden."})
    except Exception as e:
        logging.error(f"Twitter-API Fehler: {str(e)}")
        return jsonify({"status": "❌ Fehler", "message": str(e)}), 500

# --- Hauptausführung ---

if __name__ == '__main__':
    public_url = ngrok.connect(5000).public_url
    print("🔗 ngrok URL:", public_url)
    app.run()

