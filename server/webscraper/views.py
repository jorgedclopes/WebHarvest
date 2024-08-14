from datetime import datetime, timedelta
import hashlib
import json
import logging
import threading
import time
import functools
import redis

from django.http import JsonResponse
from elasticsearch import Elasticsearch
from requests import HTTPError
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.common import NoSuchElementException
from selenium.webdriver import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
import requests
from bs4 import BeautifulSoup as BS

es = Elasticsearch("http://192.168.1.4:9200/")

logger = logging.getLogger(__name__)
logger.setLevel(level=logging.INFO)

logger.info("Info message")
logger.warning("Warning message")


def fetch_info(url):
    def find_or(s, c, default="{} was not found"):
        try:
            return s.find(class_=c).text
        except AttributeError as e:
            logger.warning(f"{e} in {c}")
            return default.format(c)

    def get_description(default="N/A"):
        try:
            content = soup.find("div", class_="description__text description__text--rich").prettify()
            return content
        except Exception as e:
            logger.warning(e)
            return default

    response = requests.get(url)
    try:
        response.raise_for_status()
    except HTTPError:
        time.sleep(10)
        fetch_info(url)

    soup = BS(response.text, "html.parser")
    if not find_or(soup, "li-footer__list", ""):
        fetch_info(url)

    title = find_or(soup, "topcard__title", "")
    company = find_or(soup, "topcard__org-name-link", "").lstrip().rstrip()
    location = find_or(soup, "topcard__flavor topcard__flavor--bullet", "").lstrip().rstrip()

    description = get_description("")

    return {
        "url": url,
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "source": "LinkedIn",
        "ttl": int((timedelta(weeks=1) + datetime.utcnow()).timestamp())
    }


def log_status(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger.info(f"Starting {func=}")
        result = func(*args, **kwargs)
        # Set the status to "finished"
        logger.info(f"Finished {func=}")
        return result

    return wrapper


# Initialize the Redis client
redis_client = redis.from_url(url="redis://default:my_password@192.168.1.4:6379")


def wrap(platform):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = str(kwargs)
            try:
                # Set the status to "started"
                redis_client.set(key, json.dumps({platform: "running"}))
                result = func(*args, **kwargs)
                # Set the status to "finished"
                value = json.loads(redis_client.get(key))
                value.update({platform: "finished"})
                redis_client.set(key, json.dumps(value), ex=60 * 60)
                if all(v == "finished" for v in value.values()):
                    redis_client.set(key, json.dumps(value), ex=60 * 60)
                return result
            except Exception as e:
                # Set the status to "failed"
                redis_client.set(key, 'failed')
                raise e
        return wrapper
    return decorator


# Create your views here.
@log_status
@wrap("linkedin")
def fetch_linkedIn(index, job, location):
    logger.info(f"Fetching {job} in {location}.")
    options = Options()
    options.add_argument("--headless")
    browser = webdriver.Chrome(options=options)
    browser.implicitly_wait(5)
    browser.get('https://www.linkedin.com/jobs/search')

    def wait(n=5):
        try:
            WebDriverWait(browser, 5).until(
                EC.visibility_of_any_elements_located((By.CLASS_NAME, "jobs-search__results-list"))
            )
        except Exception as e:
            if n == 0:
                raise
            logger.warning(e)
            wait(n - 1)

    reject_button = browser.find_element(
        By.XPATH,
        value='//*[@id="artdeco-global-alert-container"]/div/section/div/div[2]/button[2]'
    )

    reject_button.click()
    try:
        clickable = browser.find_element(by=By.CLASS_NAME, value="search-bar__placeholder")
        clickable.click()
    except Exception as e:
        logger.warning(e)

    try:
        job_title_input = browser.find_element(by=By.ID, value="job-search-bar-keywords")
        country_input = browser.find_element(by=By.ID, value="job-search-bar-location")

        job_title_input.send_keys(job)
        country_input.send_keys(Keys.BACKSPACE * 20)
        country_input.send_keys(location)
        country_input.send_keys(Keys.ENTER)
        logger.info(browser.current_url)
    except Exception as e:
        logger.warning(e)
        return fetch_linkedIn(index, job, location)

    elements = browser.find_element(
        by=By.CLASS_NAME,
        value="jobs-search__results-list"
    ).find_elements(
        by=By.TAG_NAME,
        value="li"
    )

    def get_urls(element):
        try:
            return element.find_element(
                By.CLASS_NAME,
                value="base-card__full-link"
            ).get_attribute("href")
        except NoSuchElementException:
            return None

    jobs_url_optionals = [get_urls(el) for el in elements]
    jobs_url = [url for url in jobs_url_optionals if url]

    for url in jobs_url:
        logger.info(f"Processing {url}")

        info = fetch_info(url)
        if len([el for el in info.values() if el]) <= 2:
            logger.warning(f"Skipping for {url=}.")
            continue
        logger.debug(json.dumps(info, indent=4))
        data_to_hash = {k: v for k, v in info.items() if k != "ttl"}
        h = hashlib.sha256(json.dumps(data_to_hash, sort_keys=True).encode("utf-8")).hexdigest()
        es.index(index=index, document=info, id=h)


def scrape(a):
    print(a)
    key = {
        "index": "jobs",
        "job": a.GET.get("job", ""),
        "location": a.GET.get("location", "")
    }

    if redis_client.get(str(key)):
        logger.warning(f"{key} is already running")
        return JsonResponse({'message': 'Scraping already submitted.'}, status=208)

    # from django_q.tasks import async_task
    # async_task(
    #   fetch_linkedIn,
    #   index="jobs",
    #   job=job,
    #   location=location
    # )
    thread = threading.Thread(
        target=lambda: fetch_linkedIn(
            **key
        ),
        name=str(key),
    )
    thread.start()

    time.sleep(1)
    print([t.name for t in threading.enumerate()])
    return JsonResponse({'message': 'Scraping submitted and indexed started.'}, status=202)
