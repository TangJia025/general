# 在10万行日志中定位错误
grep -E "ERROR|FATAL" app.log | awk '{print $4, $3}' | sort | uniq -c | sort -nr

# 务实的程序员直接在终端敲下一行命令：
cat access.log | grep "/api/v1/payment" | grep " 403 " | awk '{print $1}' | sort | uniq -c | sort -nr | head -n 3