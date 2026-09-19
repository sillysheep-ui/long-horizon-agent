# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: json转markdown 
# --------------------------------------------

def json2md(json_block: dict, depth: int = 1, htag: str = "#") -> str:
    def parseJSON(json_block, depth):
        if isinstance(json_block, dict):
            parseDict(json_block, depth)
        if isinstance(json_block, list):
            parseList(json_block, depth)

    def parseDict(d, depth):
        for k in d:
            if isinstance(d[k], (dict, list)):
                addHeader(k, depth)
                parseJSON(d[k], depth + 1)
            else:
                addValue(k, d[k])

        nonlocal markdown
        markdown += "\n"

    def parseList(l, depth):
        for i, value in enumerate(l):
            addHeader(str(i + 1), depth)

            if not isinstance(value, (dict, list)):
                index = l.index(value)
                addValue(index, value)
            else:
                parseDict(value, depth)

        nonlocal markdown
        markdown += "\n"

    def buildHeaderChain(depth, title):
        chain = "\n" + htag * (depth + 1) + f" {title}\n\n"
        return chain

    def buildValueChain(key, value):
        chain = str(key) + f": {value}\n"
        return chain

    def addHeader(value, depth):
        chain = buildHeaderChain(depth, value.title())
        nonlocal markdown
        markdown += chain

    def addValue(key, value):
        chain = buildValueChain(key, value)
        nonlocal markdown
        markdown += chain

    markdown = ""
    parseJSON(json_block, depth)
    return markdown.strip()
