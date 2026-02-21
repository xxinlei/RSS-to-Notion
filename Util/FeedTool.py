import feedparser
from bs4 import BeautifulSoup, NavigableString, Tag
from xml.etree import ElementTree as ET
import opencc

import re
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser
import time

now = datetime.now(timezone.utc)
load_time = 60  # 導入60天內的內容

_converter = opencc.OpenCC('s2twp')  # 簡體 → 台灣繁體（含慣用詞）

def to_traditional(text: str) -> str:

    if not text:
        return text
    return _converter.convert(text)


def _rich_text(text: str, href: str = None) -> dict:
    """產生單一 rich_text 物件，文字超過 2000 字自動截斷"""
    obj = {
        "type": "text",
        "text": {"content": text[:2000]},
    }
    if href:
        obj["text"]["link"] = {"url": href}
    return obj


def _paragraph_block(rich_texts: list) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": rich_texts}}


def _heading_block(level: int, text: str) -> dict:
    tag = f"heading_{level}"
    return {
        "type": tag,
        tag: {"rich_text": [_rich_text(text)]}
    }


def _divider_block() -> dict:
    return {"type": "divider", "divider": {}}


def _bulleted_block(text: str) -> dict:
    return {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_rich_text(text)]}
    }


def _numbered_block(text: str) -> dict:
    return {
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": [_rich_text(text)]}
    }


def _parse_inline(element) -> list:
    rich_texts = []
    for child in element.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text.strip():
                rich_texts.append(_rich_text(to_traditional(text)))
        elif isinstance(child, Tag):
            if child.name == 'a':
                href = child.get('href', '')
                link_text = to_traditional(child.get_text())
                if link_text.strip():
                    rich_texts.append(_rich_text(link_text, href=href))
            else:
                text = to_traditional(child.get_text())
                if text.strip():
                    rich_texts.append(_rich_text(text))
    return rich_texts


def html_to_notion_blocks(html: str) -> list:
    if not html:
        return []

    soup = BeautifulSoup(html, 'html.parser')
    blocks = []

    for el in soup.children:
        if not isinstance(el, Tag):
            continue

        tag = el.name

        if tag == 'hr':
            blocks.append(_divider_block())

        elif tag in ('h1', 'h2'):
            text = to_traditional(el.get_text().strip())
            if text:
                blocks.append(_heading_block(2, text))

        elif tag == 'h3':
            text = to_traditional(el.get_text().strip())
            if text:
                blocks.append(_heading_block(3, text))

        elif tag in ('h4', 'h5', 'h6'):
            text = to_traditional(el.get_text().strip())
            if text:
                blocks.append(_heading_block(3, text))

        elif tag == 'p':
            rich_texts = _parse_inline(el)
            if rich_texts:
                blocks.append(_paragraph_block(rich_texts))

        elif tag in ('ul', 'ol'):
            for li in el.find_all('li', recursive=False):
                text = to_traditional(li.get_text().strip())
                if text:
                    if tag == 'ul':
                        blocks.append(_bulleted_block(text))
                    else:
                        blocks.append(_numbered_block(text))

        elif tag == 'blockquote':
            text = to_traditional(el.get_text().strip())
            if text:
                blocks.append(_paragraph_block([_rich_text(f"❝ {text}")]))

        if len(blocks) >= 99:
            break

    return blocks[:99]


def _extract_smart_title(rss_title: str, html: str) -> str:
    EPISODE_PATTERN = re.compile(
        r'(Y\d{2}W\d{2}|第\s*\d+\s*期|Issue\s*#?\d+|Vol\.?\s*\d+|\d{4}[-/]\d{2}[-/]\d{2})',
        re.IGNORECASE
    )

    if rss_title and EPISODE_PATTERN.search(rss_title):
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            for heading_tag in ('h2', 'h3'):
                first_heading = soup.find(heading_tag)
                if first_heading:
                    heading_text = first_heading.get_text().strip()
                    if heading_text and len(heading_text) > 4:
                        episode_match = EPISODE_PATTERN.search(rss_title)
                        episode = episode_match.group(0) if episode_match else ""
                        combined = f"[{episode}] {heading_text}" if episode else heading_text
                        return to_traditional(combined)

    return to_traditional(rss_title) if rss_title else "（無標題）"


# ──────────────────────────────────────────
# RSS 解析主函數
# ──────────────────────────────────────────

def parse_rss_entries(url, retries=3):
    feeds = []
    for attempt in range(retries):
        try:
            res = requests.get(
                url=url,
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.55 Safari/537.36 Edg/96.0.1054.34"},
                timeout=15,
            )
            error_code = 0
        except requests.exceptions.ProxyError as e:
            print(f"Load {url} Error, Attempt {attempt + 1} failed: {e}")
            time.sleep(1)
            error_code = 1
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as e:
            print(f"Load {url} Timeout, Attempt {attempt + 1} failed: {e}")
            time.sleep(1)
            error_code = 1

        if error_code == 0:
            parsed_feed = feedparser.parse(res.content)
            soup = BeautifulSoup(res.content, 'xml')

            # 解析 content:encoded
            ns = {'content': 'http://purl.org/rss/1.0/modules/content/'}
            content_map = {}
            try:
                root = ET.fromstring(res.content)
                for item in root.findall('./channel/item'):
                    link_el = item.find('link')
                    link_text = link_el.text if link_el is not None else None
                    if not link_text and link_el is not None:
                        link_text = link_el.tail
                    content_el = item.find('content:encoded', ns)
                    if content_el is not None and link_text:
                        content_map[link_text.strip()] = content_el.text or ""
            except Exception as e:
                print(f"解析 content:encoded 失敗: {e}")

            feed_title = soup.find('title').text if soup.find('title') else 'No title available'
            feeds = {
                "title": to_traditional(feed_title),
                "link": url,
                "status": "Active"
            }

            entries = []
            for entry in parsed_feed.entries:
                if entry.get("published"):
                    published_time = parser.parse(entry.get("published"))
                else:
                    published_time = datetime.now(timezone.utc)
                if not published_time.tzinfo:
                    published_time = published_time.replace(tzinfo=timezone(timedelta(hours=8)))

                if now - published_time < timedelta(days=load_time):
                    summary_html = entry.get("summary", "")
                    cover_soup = BeautifulSoup(summary_html, 'html.parser')
                    cover_list = cover_soup.find_all('img')
                    src = "https://www.notion.so/images/page-cover/rijksmuseum_avercamp_1620.jpg" \
                        if not cover_list else cover_list[0]['src']

                    entry_link = entry.get("link", "").strip()
                    full_html = content_map.get(entry_link, "") or summary_html

                    smart_title = _extract_smart_title(entry.get("title", ""), full_html)

                    plain_summary = to_traditional(
                        re.sub(r"<.*?>|\n+", " ", summary_html).strip()
                    )[:500]

                    entries.append({
                        "title": smart_title,
                        "link": entry_link,
                        "time": published_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "summary": plain_summary,
                        "full_html": full_html,
                        "cover": src
                    })

            return feeds, entries[:50]

    feeds = {"title": "Unknown", "link": url, "status": "Error"}
    return feeds, None


# ──────────────────────────────────────────
# Notion API
# ──────────────────────────────────────────

class NotionAPI:
    NOTION_API_pages = "https://api.notion.com/v1/pages"
    NOTION_API_database = "https://api.notion.com/v1/databases"

    def __init__(self, secret, read, feed) -> None:
        self.reader_id = read
        self.feeds_id = feed
        self.headers = {
            "Authorization": f"Bearer {secret}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    def queryFeed_from_notion(self):
        url = f"{self.NOTION_API_database}/{self.feeds_id}/query"
        payload = {
            "page_size": 100,
            "filter": {
                "property": "Disabled",
                "checkbox": {"equals": False},
            }
        }
        response = requests.post(url, headers=self.headers, json=payload)

        if response.status_code != 200:
            raise Exception(f"Failed to query Notion database: {response.text}")

        data = response.json()
        rss_feed_list = []
        for page in data['results']:
            props = page["properties"]
            multi_select = props["Tag"]["multi_select"]
            name_color_pairs = [(item['name'], item['color']) for item in multi_select]
            rss_feed_list.append({
                "url": props["URL"]["url"],
                "page_id": page.get("id"),
                "tags": name_color_pairs
            })
        return rss_feed_list

    def saveEntry_to_notion(self, entry, page_id, tags):
        # HTML → Notion Blocks
        children_blocks = html_to_notion_blocks(entry.get("full_html", ""))

       
        if not children_blocks:
            summary = entry.get("summary", "")
            if summary:
                children_blocks = [{
                    "type": "paragraph",
                    "paragraph": {"rich_text": [_rich_text(summary[:2000])]}
                }]

        payload = {
            "parent": {"database_id": self.reader_id},
            "cover": {
                "type": "external",
                "external": {"url": entry.get("cover")}
            },
            "properties": {
                "Name": {
                    "title": [{"type": "text", "text": {"content": entry.get("title", "")}}]
                },
                "URL": {"url": entry.get("link")},
                "Published": {"date": {"start": entry.get("time")}},
                "Source": {"relation": [{"id": page_id}]},
                "Tag": {
                    "multi_select": [{"name": tag[0], "color": tag[1]} for tag in tags]
                }
            },
            "children": children_blocks,
        }
        res = requests.post(url=self.NOTION_API_pages, headers=self.headers, json=payload)
        print(res.status_code)
        return res

    def saveFeed_to_notion(self, prop, page_id):
        url = f"{self.NOTION_API_pages}/{page_id}"
        payload = {
            "parent": {"database_id": self.feeds_id},
            "properties": {
                "Feed Name": {
                    "title": [{"type": "text", "text": {"content": prop.get("title", "")}}]
                },
                "Status": {
                    "select": {
                        "name": prop.get("status"),
                        "color": "red" if prop.get("status") == "Error" else "green"
                    }
                }
            },
        }
        res = requests.patch(url=url, headers=self.headers, json=payload)
        print(res.status_code)
        return res
