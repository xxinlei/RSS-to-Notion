import feedparser
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET

import re
import json
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser
import time

now = datetime.now(timezone.utc)
load_time = 60  # 导入60天内的内容


def parse_rss_entries(url, retries=3):
	entries = []
	feeds = []
	for attempt in range(retries):
		try:
			res = requests.get(
				url=url,
				headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.55 Safari/537.36 Edg/96.0.1054.34"},
			)
			error_code = 0
		except requests.exceptions.ProxyError as e:
			print(f"Load {url} Error, Attempt {attempt + 1} failed: {e}")
			time.sleep(1)
			error_code = 1
		except requests.exceptions.ConnectTimeout as e:
			print(f"Load {url} Timeout, Attempt {attempt + 1} failed: {e}")
			time.sleep(1)
			error_code = 1

		if error_code == 0:
			parsed_feed = feedparser.parse(res.content)
			soup = BeautifulSoup(res.content, 'xml')


			ns = {'content': 'http://purl.org/rss/1.0/modules/content/'}
			content_map = {}
			try:
				root = ET.fromstring(res.content)
				for item in root.findall('./channel/item'):
					link_el = item.find('link')
					# link 在 RSS 裡有時是 text，有時是 tail
					link_text = link_el.text if link_el is not None else None
					if link_text is None and link_el is not None:
						link_text = link_el.tail
					content_el = item.find('content:encoded', ns)
					if content_el is not None and link_text:
						content_map[link_text.strip()] = content_el.text or ""
			except Exception as e:
				print(f"解析 content:encoded 失敗: {e}")

			## Update RSS Feed Status
			feed_title = soup.find('title').text if soup.find('title') else 'No title available'
			feeds = {
				"title": feed_title,
				"link": url,
				"status": "Active"
			}

			for entry in parsed_feed.entries:
				if entry.get("published"):
					published_time = parser.parse(entry.get("published"))
				else:
					published_time = datetime.now(timezone.utc)
				if not published_time.tzinfo:
					published_time = published_time.replace(tzinfo=timezone(timedelta(hours=8)))
				if now - published_time < timedelta(days=load_time):
					cover = BeautifulSoup(entry.get("summary"), 'html.parser')
					cover_list = cover.find_all('img')
					src = "https://www.notion.so/images/page-cover/rijksmuseum_avercamp_1620.jpg" if not cover_list else cover_list[0]['src']

					entry_link = entry.get("link", "").strip()

			
					full_html = content_map.get(entry_link, "")
					if full_html:
						full_text = re.sub(r"<.*?>|\n*", "", full_html)
					else:
						full_text = re.sub(r"<.*?>|\n*", "", entry.get("summary", ""))

					entries.append(
						{
							"title": entry.get("title"),
							"link": entry_link,
							"time": published_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S%z"),
							"summary": re.sub(r"<.*?>|\n*", "", entry.get("summary", ""))[:500],  
							"full_text": full_text, 
							"cover": src
						}
					)

			return feeds, entries[:50]

	feeds = {
		"title": "Unknown",
		"link": url,
		"status": "Error"
	}
	return feeds, None


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
		rss_feed_list = []
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
			rss_feed_list.append(
				{
					"url": props["URL"]["url"],
					"page_id": page.get("id"),
					"tags": name_color_pairs
				}
			)
		return rss_feed_list

	def _split_text_blocks(self, text, block_size=2000):
		"""Notion 單個 rich_text 最多 2000 字，超過需要切割成多個 paragraph"""
		blocks = []
		for i in range(0, len(text), block_size):
			chunk = text[i:i + block_size]
			blocks.append({
				"type": "paragraph",
				"paragraph": {
					"rich_text": [
						{
							"type": "text",
							"text": {"content": chunk},
						}
					]
				},
			})
		return blocks

	def saveEntry_to_notion(self, entry, page_id, tags):
		
		body_text = entry.get("full_text") or entry.get("summary") or ""

		
		children_blocks = self._split_text_blocks(body_text)[:100]

		payload = {
			"parent": {"database_id": self.reader_id},
			"cover": {
				"type": "external",
				"external": {"url": entry.get("cover")}
			},
			"properties": {
				"Name": {
					"title": [
						{
							"type": "text",
							"text": {"content": entry.get("title")},
						}
					]
				},
				"URL": {"url": entry.get("link")},
				"Published": {"date": {"start": entry.get("time")}},
				"Source": {
					"relation": [{"id": page_id}]
				},
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
					"title": [
						{
							"type": "text",
							"text": {"content": prop.get("title")},
						}
					]
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
