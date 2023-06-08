import time
import os

from func_timeout import func_timeout, FunctionTimedOut
from pyquery import PyQuery
from selenium.common import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from app.log import logger

ACCESS_DENIED_TITLES = [
    # Cloudflare
    'Access denied',
    # Cloudflare http://bitturk.net/ Firefox
    'Attention Required! | Cloudflare'
]
ACCESS_DENIED_SELECTORS = [
    # Cloudflare
    'div.cf-error-title span.cf-code-label span',
    # Cloudflare http://bitturk.net/ Firefox
    '#cf-error-details div.cf-error-overview h1'
]
CHALLENGE_TITLES = [
    # Cloudflare
    'Just a moment...',
    '请稍候…',
    # DDoS-GUARD
    'DDOS-GUARD',
]
CHALLENGE_SELECTORS = [
    # Cloudflare
    '#cf-challenge-running', '.ray_id', '.attack-box', '#cf-please-wait', '#challenge-spinner', '#trk_jschal_js',
    # Custom CloudFlare for EbookParadijs, Film-Paleis, MuziekFabriek and Puur-Hollands
    'td.info #js_info',
    # Fairlane / pararius.com
    'div.vc div.text-box h2'
]
SHORT_TIMEOUT = 6
CF_TIMEOUT = int(os.getenv("NASTOOL_CF_TIMEOUT", "60"))


def resolve_challenge(driver: WebDriver, timeout=CF_TIMEOUT):
    start_ts = time.time()
    try:
        func_timeout(timeout, _evil_logic, args=(driver,))
        return True
    except FunctionTimedOut:
        logger.error(f'Error solving the challenge. Timeout {timeout} after {round(time.time() - start_ts, 1)} seconds.')
        return False
    except Exception as e:
        logger.error('Error solving the challenge. ' + str(e))
        return False


def under_challenge(html_text: str):
    """
    Check if the page is under challenge
    :param html_text:
    :return:
    """
    # get the page title
    if not html_text:
        return False
    page_title = PyQuery(html_text)('title').text()
    logger.debug("under_challenge page_title=" + page_title)
    for title in CHALLENGE_TITLES:
        if page_title.lower() == title.lower():
            return True
    for selector in CHALLENGE_SELECTORS:
        html_doc = PyQuery(html_text)
        if html_doc(selector):
            return True
    return False


def _until_title_changes(driver: WebDriver, titles):
    WebDriverWait(driver, SHORT_TIMEOUT).until_not(lambda x: _any_match_titles(x, titles))


def _any_match_titles(driver: WebDriver, titles):
    page_title = driver.title
    for title in titles:
        if page_title.lower() == title.lower():
            return True
    return False


def _until_selectors_disappear(driver: WebDriver, selectors):
    WebDriverWait(driver, SHORT_TIMEOUT).until_not(lambda x: _any_match_selectors(x, selectors))


def _any_match_selectors(driver: WebDriver, selectors):
    for selector in selectors:
        html_doc = PyQuery(driver.page_source)
        if html_doc(selector):
            return True
    return False


def _evil_logic(driver: WebDriver):
    driver.implicitly_wait(SHORT_TIMEOUT)
    # wait for the page
    html_element = driver.find_element(By.TAG_NAME, "html")

    # find access denied titles
    if _any_match_titles(driver, ACCESS_DENIED_TITLES):
        raise Exception('Cloudflare has blocked this request. '
                        'Probably your IP is banned for this site, check in your web browser.')
    # find access denied selectors
    if _any_match_selectors(driver, ACCESS_DENIED_SELECTORS):
        raise Exception('Cloudflare has blocked this request. '
                        'Probably your IP is banned for this site, check in your web browser.')

    # find challenge by title
    challenge_found = False
    if _any_match_titles(driver, CHALLENGE_TITLES):
        challenge_found = True
        logger.info("Challenge detected. Title found: " + driver.title)
    if not challenge_found:
        # find challenge by selectors
        if _any_match_selectors(driver, CHALLENGE_SELECTORS):
            challenge_found = True
            logger.info("Challenge detected. Selector found")

    attempt = 0
    if challenge_found:
        while True:
            try:
                attempt = attempt + 1
                # wait until the title changes
                _until_title_changes(driver, CHALLENGE_TITLES)

                # then wait until all the selectors disappear
                _until_selectors_disappear(driver, CHALLENGE_SELECTORS)

                # all elements not found
                break

            except TimeoutException:
                logger.debug("Timeout waiting for selector")

                click_verify(driver)

                # update the html (cloudflare reloads the page every 5 s)
                html_element = driver.find_element(By.TAG_NAME, "html")

            # waits until cloudflare redirection ends
        logger.debug("Waiting for redirect")
        # noinspection PyBroadException
        try:
            WebDriverWait(driver, SHORT_TIMEOUT).until(EC.staleness_of(html_element))
        except Exception:
            logger.debug("Timeout waiting for redirect")

        logger.info("Challenge solved!")
    else:
        logger.info("Challenge not detected!")


def click_verify(driver: WebDriver):
    try:
        logger.debug("Try to find the Cloudflare verify checkbox")
        iframe = driver.find_element(By.XPATH, "//iframe[@title='Widget containing a Cloudflare security challenge']")
        driver.switch_to.frame(iframe)
        checkbox = driver.find_element(
            by=By.XPATH,
            value='//*[@id="cf-stage"]//label[@class="ctp-checkbox-label"]/input',
        )
        if checkbox:
            actions = ActionChains(driver)
            actions.move_to_element_with_offset(checkbox, 5, 7)
            actions.click(checkbox)
            actions.perform()
            logger.debug("Cloudflare verify checkbox found and clicked")
    except Exception as e:
        logger.debug(f"Cloudflare verify checkbox not found on the page: {str(e)}")
        # print(e)
    finally:
        driver.switch_to.default_content()

    try:
        logger.debug("Try to find the Cloudflare 'Verify you are human' button")
        button = driver.find_element(
            by=By.XPATH,
            value="//input[@type='button' and @value='Verify you are human']",
        )
        if button:
            actions = ActionChains(driver)
            actions.move_to_element_with_offset(button, 5, 7)
            actions.click(button)
            actions.perform()
            logger.debug("The Cloudflare 'Verify you are human' button found and clicked")
    except Exception as e:
        logger.debug(f"The Cloudflare 'Verify you are human' button not found on the page：{str(e)}")
        # print(e)

    time.sleep(2)
