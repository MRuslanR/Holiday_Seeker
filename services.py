# services.py
import calendar
import datetime
import json
import sqlite3
from typing import List, Dict, Any, Optional
from collections import defaultdict
import config
from prompts import SYSTEM_PROMPT_DEDUPLICATE, SYSTEM_PROMPT_CHECKER

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Обновляем импорты конфига. Убедитесь, что OPENROUTER_API_KEY есть в config.py
from config import (DB_PATH, get_logger, OPENROUTER_API_KEY, API_KEY_NINJAS, DEDUPLICATE_MODEL, FILTERING_MODEL)
from utils import APIError, retry_on_exception, InvalidJSONPayloadError

logger = get_logger(__name__)


class OpenRouterClient:
    """Клиент для взаимодействия с OpenRouter API."""
    
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    @retry_on_exception(exceptions=(APIError, requests.RequestException))
    def create_chat_completion(self, system_prompt: str, user_content: str) -> Dict[str, Any]:
        """
        Отправляет запрос в OpenRouter.
        Возвращает словарь: {'result': str, 'tokens': int, 'price': float}
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
        }

        try:
            response = requests.post(self.base_url, headers=headers, json=data, timeout=60)
            response.raise_for_status()
            json_response = response.json()

            # Извлечение текста
            if not json_response.get('choices'):
                raise APIError("Пустой ответ от OpenRouter (нет choices)")
            
            content = json_response['choices'][0]['message']['content']
            usage = json_response.get('usage', {})
            total_tokens = usage.get('total_tokens', 0)
            price = usage.get("cost", 0.0) 

            return {
                'result': content,
                'tokens': total_tokens,
                'price': price
            }

        except requests.exceptions.HTTPError as e:
             raise APIError(f"HTTP ошибка OpenRouter: {e.response.status_code} {e.response.text}")
        except requests.exceptions.RequestException as e:
            raise APIError(f"Ошибка сети OpenRouter: {e}")
        except Exception as e:
            raise APIError(f"Ошибка обработки ответа OpenRouter: {e}")


class HolidayService:
    """
    Класс для сбора, обработки и сохранения информации о праздниках.
    Инкапсулирует логику взаимодействия с внешними API и базой данных.
    """

    def __init__(self):
        self.db_path = DB_PATH
        self.api_key_ninjas = API_KEY_NINJAS
        self.session = requests.Session()
        self.session.timeout = 60
        
        retry_strategy = Retry(
            total=3,                
            backoff_factor=1,       
            status_forcelist=[429, 500, 502, 503, 504], 
            allowed_methods=["HEAD", "GET", "OPTIONS"]  
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        self.logger = get_logger(self.__class__.__name__)

        self.grand_total_tokens = 0
        self.grand_total_price = 0.0

        self.logger.info("Инициализация HolidayService...")
        self._init_db()

        if not OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY не задан в конфигурации.")
            
        self.deduplicate_llm_client = OpenRouterClient(OPENROUTER_API_KEY, DEDUPLICATE_MODEL)
        self.filter_llm_client = OpenRouterClient(OPENROUTER_API_KEY, FILTERING_MODEL)

    def _init_db(self):
        log_ctx = {'service': 'DB', 'operation': 'init'}
        self.logger.info("Проверка и инициализация таблиц БД...", extra={'context': log_ctx})
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA foreign_keys = ON;")
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS holidays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    country_code TEXT NOT NULL,
                    holiday_date DATE NOT NULL,
                    holiday_name TEXT NOT NULL,
                    UNIQUE(country_code, holiday_date, holiday_name)
                )''')
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS regions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holiday_id INTEGER NOT NULL,
                    region_name TEXT NOT NULL,
                    FOREIGN KEY (holiday_id) REFERENCES holidays (id) ON DELETE CASCADE,
                    UNIQUE(holiday_id, region_name)
                )''')
                conn.commit()
                self.logger.info("Инициализация таблиц БД успешно завершена.", extra={'context': log_ctx})
        except sqlite3.Error:
            self.logger.exception("Критическая ошибка при инициализации таблиц БД.", extra={'context': log_ctx})
            raise

    def get_holidays_for_date(self, target_date: str) -> Dict[str, Dict[str, List[str]]]:
        log_ctx = {'service': 'DB', 'operation': 'get_holidays_with_regions', 'date': target_date}
        self.logger.info(f"Запрос праздников и регионов из БД на дату {target_date}", extra={'context': log_ctx})
        holidays_by_country = defaultdict(lambda: defaultdict(list))
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                query = """
                SELECT
                    h.country_code,
                    h.holiday_name,
                    r.region_name
                FROM holidays h
                LEFT JOIN regions r ON h.id = r.holiday_id
                WHERE h.holiday_date = ?
                ORDER BY h.country_code, h.holiday_name
                """
                cursor.execute(query, (target_date,))
                for country_code, holiday_name, region_name in cursor.fetchall():
                    if region_name:
                        holidays_by_country[country_code][holiday_name].append(region_name)
                    else:
                        _ = holidays_by_country[country_code][holiday_name]
            final_result = {k: dict(v) for k, v in holidays_by_country.items()}
            self.logger.info(f"Найдено праздников для {len(final_result)} стран.", extra={'context': log_ctx})
            return final_result
        except sqlite3.Error as e:
            self.logger.exception(f"Ошибка при чтении праздников из БД на дату {target_date}",
                                  extra={'context': log_ctx})
            return {}

    def _get_from_api(self, source_name: str, url: str, **kwargs) -> List[Dict[str, Any]]:
        log_ctx = {'source_api': source_name, 'url': url}
        self.logger.info(f"Запрос данных из {source_name}...", extra={'context': log_ctx})
        try:
            response = self.session.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            self.logger.error(f"Сетевая ошибка при запросе к {source_name}: {e}", extra={'context': log_ctx})
        except json.JSONDecodeError as e:
            self.logger.error(f"Ошибка декодирования JSON от {source_name}: {e}", extra={'context': log_ctx})
        return []

    def _get_from_ninjas(self, country_code: str, year: str, month: str) -> List[Dict[str, str]]:
        url = f'https://api.api-ninjas.com/v1/workingdays?country={country_code}&month={month}'
        data = self._get_from_api("API-Ninjas", url, headers={'X-Api-Key': self.api_key_ninjas})
        holidays = []
        if not data or 'non_working_days' not in data:
            return []
        for entry in data.get('non_working_days', []):
            holiday_date, reasons = entry.get('date'), entry.get('reasons')
            if holiday_date and reasons and 'weekend' not in reasons and int(holiday_date[5:7]) == int(month) and int(
                    holiday_date[:4]) == int(year):
                holidays.append({'date': holiday_date, 'name': entry.get('holiday_name', 'Unknown Holiday')})
        self.logger.info(f"Найдено {len(holidays)} праздников в API-Ninjas для {country_code}.")
        return holidays

    def _get_from_nager(self, country_code: str, year: str, month: str) -> List[Dict[str, str]]:
        url = f'https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}'
        data = self._get_from_api("Nager.Date", url)
        holidays = []
        if not isinstance(data, list):
            return []
        for entry in data:
            holiday_date = entry.get('date')
            if holiday_date and int(holiday_date[5:7]) == int(month):
                holidays.append({'date': holiday_date, 'name': entry.get('name', 'Unknown Holiday')})
        self.logger.info(f"Найдено {len(holidays)} праздников в Nager.Date для {country_code}.")
        return holidays
    
    def _is_weekend(self, date_str: str) -> bool:
        """
        Проверяет, выпадает ли дата на субботу (5) или воскресенье (6).
        Ожидает формат YYYY-MM-DD.
        """
        if not date_str:
            return False
        try:
            dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            return dt.weekday() >= 5
        except ValueError:
            self.logger.error(f"Некорректный формат даты для проверки на выходной: {date_str}")
            return False

    def _get_from_openholidays(self, country_code: str, first_day: str, last_day: str) -> List[Dict[str, str]]:
        url = "https://openholidaysapi.org/PublicHolidays"
        params = {"countryIsoCode": country_code, "languageIsoCode": "EN", "validFrom": first_day, "validTo": last_day}
        data = self._get_from_api("OpenHolidaysAPI", url, params=params, headers={"accept": "text/json"})
        holidays = []
        if not isinstance(data, list):
            return []
        for entry in data:
            if entry.get('name') and entry.get('startDate'):
                holidays.append({"date": entry['startDate'], "name": entry['name'][0]['text']})
        self.logger.info(f"Найдено {len(holidays)} праздников в OpenHolidaysAPI для {country_code}.")
        return holidays

    def _parse_llm_json_response(self, response_text: str) -> Dict[str, Any]:
        """
        Надежно извлекает JSON из ответа LLM.
        В случае неудачи выбрасывает InvalidJSONPayloadError.
        """
        log_ctx = {'service': 'LLMParser'}
        try:
            # Поиск JSON-блока даже если он обернут в Markdown ```json ... ```
            json_start_index = response_text.find('{')
            if json_start_index == -1:
                raise InvalidJSONPayloadError(f"Не найден JSON объект в ответе LLM: '{response_text}'")

            # Очистка если есть текст после JSON
            json_part = response_text[json_start_index:]
            
            # Ищем последний закрывающий символ
            json_end_index = json_part.rfind('}')
            if json_end_index == -1:
                raise InvalidJSONPayloadError(f"Не найден корректный конец JSON объекта в ответе LLM.")

            clean_json_str = json_part[:json_end_index + 1].strip()
            return json.loads(clean_json_str)

        except json.JSONDecodeError as e:
            self.logger.error(f"Не удалось декодировать JSON из ответа. Ответ: '{response_text}'",
                              extra=log_ctx, exc_info=True)
            raise InvalidJSONPayloadError(f"Ошибка декодирования JSON: {e}") from e
        except Exception as e:
            self.logger.exception(f"Непредвиденная ошибка при парсинге ответа. Ответ: '{response_text}'",
                                  extra=log_ctx)
            raise InvalidJSONPayloadError(f"Непредвиденная ошибка парсинга: {e}") from e

    def _save_verified_holiday(self, country_code: str, holiday_data: Dict[str, Any]):
        log_ctx = {'service': 'DB', 'operation': 'save', 'holiday_name': holiday_data.get('name')}
        self.logger.info(f"Сохранение проверенного праздника '{holiday_data.get('name')}' в БД.", extra=log_ctx)
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA foreign_keys = ON;")
                cursor.execute(
                    'INSERT OR IGNORE INTO holidays (country_code, holiday_date, holiday_name) VALUES (?, ?, ?)',
                    (country_code, holiday_data['date'], holiday_data['name'])
                )
                cursor.execute(
                    'SELECT id FROM holidays WHERE country_code = ? AND holiday_date = ? AND holiday_name = ?',
                    (country_code, holiday_data['date'], holiday_data['name'])
                )
                holiday_id_tuple = cursor.fetchone()
                if not holiday_id_tuple:
                    self.logger.error("Не удалось найти/создать запись о празднике, сохранение регионов отменено.",
                                      extra=log_ctx)
                    return
                holiday_id = holiday_id_tuple[0]
                regions = holiday_data.get('regions', [])
                if not regions or regions == ["All"]: # Обработка "All" как отсутствие специфического региона
                    self.logger.info("Регионы не указаны или праздник общенациональный.", extra=log_ctx)
                    return
                
                regions_to_insert = [(holiday_id, region_name) for region_name in regions]
                cursor.executemany(
                    'INSERT OR IGNORE INTO regions (holiday_id, region_name) VALUES (?, ?)',
                    regions_to_insert
                )
                conn.commit()
                self.logger.info(
                    f"Успешно сохранено {cursor.rowcount} новых регионов для праздника '{holiday_data['name']}'.",
                    extra=log_ctx)
        except sqlite3.Error:
            self.logger.exception(f"Ошибка при сохранении праздника '{holiday_data.get('name')}' в БД.", extra=log_ctx)
        except KeyError as e:
            self.logger.error(f"Отсутствует обязательное поле '{e}' в данных праздника для сохранения: {holiday_data}",
                              extra=log_ctx)
            
    @retry_on_exception(exceptions=(APIError, InvalidJSONPayloadError))
    def _get_safe_llm_response(self, client: OpenRouterClient, system_prompt: str, user_content: str) -> Dict[str, Any]:
        """
        Выполняет запрос к LLM и сразу пытается распарсить JSON.
        Если ответ не валидный JSON, выбрасывает исключение, чтобы сработал retry.
        """
        response = client.create_chat_completion(system_prompt, user_content)
        parsed_data = self._parse_llm_json_response(response['result'])
        
        return {
            'data': parsed_data,
            'tokens': response['tokens'],
            'price': response['price']
        }

    def process_holidays_for_period(self, country_code: str, year: str, month: str, first_day: str, last_day: str):
        log_ctx = {'country': country_code, 'period': f"{year}-{month}"}
        self.logger.info(f"Начало обработки праздников для страны: {country_code.upper()}", extra={'context': log_ctx})

        country_tokens = 0
        country_price = 0.0

        # Сбор сырых данных
        raw_holidays = {
            "ninjas_holidays": self._get_from_ninjas(country_code, year, month),
            "nager_holidays": self._get_from_nager(country_code, year, month),
            "open_holidays": self._get_from_openholidays(country_code, first_day, last_day)
        }
        
        # Если везде пусто
        if not any(raw_holidays.values()):
            self.logger.warning("Ни один из источников не вернул данных о праздниках. Обработка завершена.",
                                extra={'context': log_ctx})
            return

        holidays_to_check = []
        try:
            self.logger.info("Отправка данных на дедупликацию в OpenRouter...", extra={'context': log_ctx})
            
            dedup_user_content = json.dumps(raw_holidays, ensure_ascii=False)

            dedup_response = self._get_safe_llm_response(
                self.deduplicate_llm_client,
                SYSTEM_PROMPT_DEDUPLICATE,
                dedup_user_content
            )

            # Обновляем статистику
            current_tokens = dedup_response['tokens']
            current_price = dedup_response['price']
            
            country_tokens += current_tokens
            country_price += current_price
            self.grand_total_tokens += current_tokens
            self.grand_total_price += current_price

            self.logger.info(f"[Экономика] Дедупликация: {current_tokens} токенов, {current_price:.4f}$")

            # Получаем уже чистые данные (парсинг прошел успешно внутри _get_safe_llm_response)
            clean_holidays_data = dedup_response['data']
            holidays_to_check = clean_holidays_data.get('holidays', [])
            
            self.logger.info(
                f"Дедупликация завершена. Получено {len(holidays_to_check)} уникальных праздников для проверки.",
                extra={'context': log_ctx})

        except (APIError, InvalidJSONPayloadError) as e:
            # Сюда мы попадем, только если ПОСЛЕ ВСЕХ ПОПЫТОК (retries) так и не удалось получить JSON
            self.logger.exception(
                "Не удалось выполнить дедупликацию даже после нескольких попыток. Обработка страны прервана.",
                extra={'context': log_ctx})
            return

        if not holidays_to_check:
            self.logger.info("После дедупликации не осталось праздников для проверки.", extra={'context': log_ctx})
        else:
            self.logger.info("Начало проверки фактов и сохранения праздников...", extra={'context': log_ctx})
            
            for holiday in holidays_to_check:
                try:
                    date_str = holiday.get('date')
                    # Проверка на выходные
                    if self._is_weekend(date_str):
                        self.logger.info(
                            f"Праздник '{holiday.get('name')}' ({date_str}) выпадает на выходной. Пропускаем проверку.",
                            extra={'context': log_ctx}
                        )
                        continue
                    
                    holiday['region_context'] = country_code
                    checker_user_content = json.dumps(holiday, ensure_ascii=False)
                    
                    checker_response = self._get_safe_llm_response(
                        self.filter_llm_client,
                        SYSTEM_PROMPT_CHECKER,
                        checker_user_content
                    )

                    current_tokens = checker_response['tokens']
                    current_price = checker_response['price']
                    
                    country_tokens += current_tokens
                    country_price += current_price
                    self.grand_total_tokens += current_tokens
                    self.grand_total_price += current_price
                    
                    self.logger.info(
                        f"[Экономика] Проверка '{holiday.get('name')}': {current_tokens} токенов, {current_price:.4f}$.")

                    verified_data = checker_response['data']

                    # Логика сохранения результата
                    is_holiday_flag = verified_data.get('is_holiday')
                    if str(is_holiday_flag).lower() == 'true' or is_holiday_flag is True:
                        self.logger.info(f"Праздник '{holiday.get('name')} - {holiday.get('date')}' подтвержден.")
                        self._save_verified_holiday(country_code, verified_data)
                    else:
                        self.logger.info(
                            f"Праздник '{holiday.get('name')} - {holiday.get('date')}' НЕ является выходным.")

                except (APIError, InvalidJSONPayloadError) as e:
                    self.logger.error(
                        f"Не удалось проверить праздник '{holiday.get('name')}' после всех попыток (ошибка JSON/API). Пропускаем.",
                        extra={'context': log_ctx})
                    continue
                except Exception as e:
                    self.logger.exception(f"Непредвиденная ошибка при обработке праздника: {holiday}. Пропускаем.", extra={'context': log_ctx})
                    continue

        # Итоговая статистика по стране
        self.logger.info(f"Итоги по экономике для страны {country_code.upper()}:")
        self.logger.info(f"  - Потрачено токенов: {country_tokens}")
        self.logger.info(f"  - Общая стоимость: {country_price:.4f}$")
        self.logger.info(f"Обработка праздников для страны {country_code.upper()} завершена.", extra={'context': log_ctx})
