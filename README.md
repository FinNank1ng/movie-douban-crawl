
# movie-douban-crawl
![License](https://img.shields.io/badge/license-Apache-yellow)
![Language](https://img.shields.io/badge/language-python-brightgreen)
## 简介

- 使用了requests与BS4，使用了douban_api模块进行首次使用，对豆瓣网站进行尝试性分段式+全量爬取（非自动化），带有运行log与jsonl保存对流量进行控制

### 目前功能
- 对前250名的电影进行全量爬取
- 对前3页最受欢迎的影评进行爬取
- 添加了API——JSON申请访问（非登录）
- 添加了限流与重试功能（非自动化）
- 分段式爬取与缓存读取

## 说明与警告
- 对于该小项目，在目标代码结构的不断变化，并不能保证在未来100%有效，目前公开表明并不继续维护本项目
- 代码由本人全全原创，且爬取的内容以及代码内容不可变卖！
- 代码可进行二次创作（内容相似到60%左右以上依旧必须标注二创与来源）
- 代码的获取逻辑可改，但传播也必须标注来源！


通常情况下，你不需要修改这个文件。因为它已经正常工作！除非网站的HTML结构已经改变！
