"""Feeds a new reader's first profile can pick from (each checked to work,
October 2026). Readers can add any other feed by hand, or give their own in
the interview."""

CATALOGUE = [
    # General news
    {"name": "ABC News Top Stories", "url": "https://www.abc.net.au/news/feed/45910/rss.xml", "about": "Australian national news, the big stories"},
    {"name": "ABC News Just In", "url": "https://www.abc.net.au/news/feed/51120/rss.xml", "about": "Australian news as it happens, everything"},
    {"name": "ABC News World", "url": "https://www.abc.net.au/news/feed/104217382/rss.xml", "about": "World news from the ABC"},
    {"name": "SBS News", "url": "https://www.sbs.com.au/news/feed", "about": "Australian and world news from SBS"},
    {"name": "The Guardian Australia", "url": "https://www.theguardian.com/au/rss", "about": "Australian edition: news, politics, opinion"},
    {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "about": "World news from the BBC"},
    {"name": "Sydney Morning Herald", "url": "https://www.smh.com.au/rss/feed.xml", "about": "Sydney and national news"},
    {"name": "WA Today", "url": "https://www.watoday.com.au/rss/feed.xml", "about": "Perth and Western Australia news"},
    {"name": "The Conversation AU", "url": "https://www.theconversation.com/au/articles.atom", "about": "Academics explaining research and current affairs"},
    # Science
    {"name": "ScienceAlert", "url": "https://www.sciencealert.com/feed", "about": "Popular science news"},
    {"name": "NASA", "url": "https://www.nasa.gov/feed/", "about": "Space and NASA news"},
    # Technology
    {"name": "Hacker News Best", "url": "https://hnrss.org/best", "about": "The best of Hacker News: tech, startups, programming"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "about": "In-depth technology, science and policy"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "about": "Consumer tech and culture"},
    {"name": "WIRED", "url": "https://www.wired.com/feed/rss", "about": "Technology, science and culture"},
    {"name": "Engadget", "url": "https://www.engadget.com/rss.xml", "about": "Gadgets and consumer tech"},
    {"name": "9to5Mac", "url": "https://9to5mac.com/feed/", "about": "Apple news"},
    {"name": "Hackaday", "url": "https://hackaday.com/blog/feed/", "about": "Hardware hacking, electronics, maker projects"},
    {"name": "CNX Software", "url": "https://www.cnx-software.com/feed/", "about": "Embedded systems, single-board computers, IoT"},
]
