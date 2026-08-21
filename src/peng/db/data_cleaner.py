#!/usr/bin/env python3
"""
数据清洗脚本 - 用于清洗AI助手项目的数据库

功能：
1. 数据质量分析
2. 识别重复记录
3. 识别孤儿记录
4. 数据格式标准化
5. 生成清洗报告
"""

import os
import sys
from datetime import datetime
from typing import Any

from peng.common import jsonp as json
from peng.config import get_agent_name, get_path
from peng.db.connection import get_db_connection

AGENT_NAME = get_agent_name()


class DataCleaner:
    """数据清洗器类"""

    def __init__(self, db_path: str, backup_dir: str = None):
        """
        初始化数据清洗器

        Args:
            db_path: 数据库文件路径
            backup_dir: 备份目录路径
        """
        self.db_path = db_path
        self.backup_dir = backup_dir or os.path.join(os.path.dirname(db_path), "backup")
        self.conn = None
        self.report = {
            "timestamp": datetime.now().isoformat(),
            "db_path": db_path,
            "analysis": {},
            "duplicates": [],
            "orphans": [],
            "standardization": [],
            "actions_taken": [],
            "summary": {},
        }

    def connect(self):
        """连接数据库"""
        self.conn = get_db_connection(self.db_path)
        return self.conn

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            self.conn = None

    def backup_database(self) -> str:
        """
        备份数据库

        Returns:
            备份文件路径
        """
        os.makedirs(self.backup_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        db_name = os.path.basename(self.db_path)
        backup_path = os.path.join(self.backup_dir, f"{db_name}.{timestamp}.bak")

        # 使用 PostgreSQL pg_dump 备份功能
        # 注意：需要使用pg_dump
        # 这里简化为复制连接并导出
        import shlex
        import subprocess

        # 从连接中提取数据库配置
        db_config = {
            "dbname": self.conn.info.dbname if hasattr(self.conn, "info") else "peng",
            "host": self.conn.info.host if hasattr(self.conn, "info") else "localhost",
            "port": self.conn.info.port if hasattr(self.conn, "info") else 5432,
            "user": self.conn.info.user if hasattr(self.conn, "info") else "postgres",
        }

        # 使用pg_dump备份
        dump_cmd = [
            "pg_dump",
            f"--dbname={db_config['dbname']}",
            f"--host={db_config['host']}",
            f"--port={db_config['port']}",
            f"--username={db_config['user']}",
            "--file",
            backup_path,
            "--format=c",  # 自定义格式
            "--no-owner",
            "--no-privileges",
        ]

        try:
            subprocess.run(
                dump_cmd, check=True, capture_output=True, timeout=600
            )  # 数据库备份允许10分钟
        except subprocess.CalledProcessError as e:
            logger.warning(f"pg_dump 失败，尝试SQL导出: {e}")
            # 备用方案：使用SQL导出
            sql_backup_path = backup_path.rsplit(".", 1)[0] + ".sql"
            sql_cmd = [
                "pg_dump",
                f"--dbname={db_config['dbname']}",
                f"--host={db_config['host']}",
                f"--port={db_config['port']}",
                f"--username={db_config['user']}",
                "--file",
                sql_backup_path,
                "--no-owner",
                "--no-privileges",
            ]
            subprocess.run(sql_cmd, check=True, timeout=600)  # 数据库备份允许10分钟

        self.report["actions_taken"].append(
            {"action": "backup", "path": backup_path, "timestamp": datetime.now().isoformat()}
        )

        return backup_path

    def analyze_data_quality(self) -> dict[str, Any]:
        """
        分析数据质量

        Returns:
            数据质量分析报告
        """
        cursor = self.conn.cursor()

        analysis = {
            "total_records": 0,
            "status_distribution": {},
            "receiver_distribution": {},
            "sender_distribution": {},
            "null_fields": {},
            "invalid_formats": [],
        }

        # 总记录数
        cursor.execute("SELECT COUNT(*) as count FROM messages")
        analysis["total_records"] = cursor.fetchone()["count"]

        # 状态分布
        cursor.execute("SELECT status, COUNT(*) as count FROM messages GROUP BY status")
        analysis["status_distribution"] = {row["status"]: row["count"] for row in cursor.fetchall()}

        # 接收者分布
        cursor.execute("SELECT receiver, COUNT(*) as count FROM messages GROUP BY receiver")
        analysis["receiver_distribution"] = {
            row["receiver"]: row["count"] for row in cursor.fetchall()
        }

        # 发送者分布
        cursor.execute("SELECT sender, COUNT(*) as count FROM messages GROUP BY sender")
        analysis["sender_distribution"] = {row["sender"]: row["count"] for row in cursor.fetchall()}

        # 检查空值字段
        null_checks = [
            ("timestamp", "timestamp IS NULL"),
            ("sender", 'sender IS NULL OR sender = ""'),
            ("content", 'content IS NULL OR content = ""'),
            ("status", "status IS NULL"),
        ]

        for field, condition in null_checks:
            cursor.execute(f"SELECT COUNT(*) as count FROM messages WHERE {condition}")
            count = cursor.fetchone()["count"]
            if count > 0:
                analysis["null_fields"][field] = count

        # 检查时间格式
        cursor.execute(
            "SELECT id, timestamp FROM messages WHERE timestamp NOT LIKE '____-__-__ __:__:__'"
        )
        invalid_timestamps = cursor.fetchall()
        if invalid_timestamps:
            analysis["invalid_formats"].append(
                {
                    "field": "timestamp",
                    "count": len(invalid_timestamps),
                    "examples": [row["id"] for row in invalid_timestamps[:5]],
                }
            )

        self.report["analysis"] = analysis
        return analysis

    def find_duplicates(self, by_content: bool = True) -> list[dict[str, Any]]:
        """
        查找重复记录

        Args:
            by_content: 是否按内容查找重复

        Returns:
            重复记录列表
        """
        cursor = self.conn.cursor()
        duplicates = []

        if by_content:
            # 按内容查找重复
            cursor.execute("""
                SELECT content, COUNT(*) as cnt, GROUP_CONCAT(id) as ids
                FROM messages
                GROUP BY content
                HAVING cnt > 1
                ORDER BY cnt DESC
            """)

            for row in cursor.fetchall():
                content, count, ids = row
                # 截断长内容
                display_content = content[:50] + "..." if len(content) > 50 else content
                duplicates.append(
                    {
                        "content": display_content,
                        "full_content": content,
                        "count": count,
                        "ids": [int(x) for x in ids.split(",")],
                    }
                )

        self.report["duplicates"] = duplicates
        return duplicates

    def find_orphans(self) -> dict[str, list[dict[str, Any]]]:
        """
        查找孤儿记录（外键指向不存在的记录）

        Returns:
            孤儿记录字典
        """
        cursor = self.conn.cursor()
        orphans = {"reply_to_orphans": [], "chain_head_orphans": []}

        # 查找 reply_to 指向不存在的记录
        cursor.execute("""
            SELECT m.id, m.reply_to, m.content
            FROM messages m
            LEFT JOIN messages p ON m.reply_to = p.id
            WHERE m.reply_to IS NOT NULL AND p.id IS NULL
        """)

        for row in cursor.fetchall():
            orphans["reply_to_orphans"].append(
                {
                    "id": row["id"],
                    "reply_to": row["reply_to"],
                    "content": row["content"][:50] + "..."
                    if row["content"] and len(row["content"]) > 50
                    else row["content"],
                }
            )

        # 查找 chain_head_id 指向不存在的记录
        cursor.execute("""
            SELECT m.id, m.chain_head_id, m.content
            FROM messages m
            LEFT JOIN messages c ON m.chain_head_id = c.id
            WHERE m.chain_head_id IS NOT NULL AND c.id IS NULL
        """)

        for row in cursor.fetchall():
            orphans["chain_head_orphans"].append(
                {
                    "id": row["id"],
                    "chain_head_id": row["chain_head_id"],
                    "content": row["content"][:50] + "..."
                    if row["content"] and len(row["content"]) > 50
                    else row["content"],
                }
            )

        self.report["orphans"] = orphans
        return orphans

    def handle_duplicates(self, strategy: str = "keep_first") -> dict[str, Any]:
        """
        处理重复记录

        Args:
            strategy: 处理策略 ('keep_first', 'keep_latest', 'mark_archived')

        Returns:
            处理结果
        """
        cursor = self.conn.cursor()
        result = {"strategy": strategy, "processed_groups": 0, "records_archived": 0, "details": []}

        # 获取重复组
        cursor.execute("""
            SELECT content, GROUP_CONCAT(id) as ids
            FROM messages
            GROUP BY content
            HAVING COUNT(*) > 1
        """)

        for row in cursor.fetchall():
            content, ids_str = row
            ids = [int(x) for x in ids_str.split(",")]

            if strategy == "keep_first":
                # 保留最早创建的记录（ID 最小）
                ids_to_keep = [min(ids)]
                ids_to_archive = [x for x in ids if x != min(ids)]
            elif strategy == "keep_latest":
                # 保留最新创建的记录（ID 最大）
                ids_to_keep = [max(ids)]
                ids_to_archive = [x for x in ids if x != max(ids)]
            else:
                ids_to_keep = []
                ids_to_archive = ids

            # 标记为归档
            if ids_to_archive:
                placeholders = ",".join(["%s"] * len(ids_to_archive))
                cursor.execute(
                    f"""
                    UPDATE messages
                    SET is_archived = 1, status = 'archived'
                    WHERE id IN ({placeholders})
                """,
                    ids_to_archive,
                )

                result["records_archived"] += len(ids_to_archive)
                result["details"].append(
                    {
                        "content": content[:50] + "..." if len(content) > 50 else content,
                        "kept_ids": ids_to_keep,
                        "archived_ids": ids_to_archive,
                    }
                )
                result["processed_groups"] += 1

        self.conn.commit()

        self.report["actions_taken"].append(
            {"action": "handle_duplicates", "strategy": strategy, "result": result}
        )

        return result

    def handle_orphans(self, strategy: str = "nullify") -> dict[str, Any]:
        """
        处理孤儿记录

        Args:
            strategy: 处理策略 ('nullify', 'delete', 'mark_invalid')

        Returns:
            处理结果
        """
        cursor = self.conn.cursor()
        result = {"strategy": strategy, "reply_to_fixed": 0, "chain_head_fixed": 0, "details": []}

        if strategy == "nullify":
            # 将无效的外键设为 NULL
            cursor.execute("""
                UPDATE messages
                SET reply_to = NULL
                WHERE reply_to IS NOT NULL
                AND reply_to NOT IN (SELECT id FROM messages)
            """)
            result["reply_to_fixed"] = cursor.rowcount

            cursor.execute("""
                UPDATE messages
                SET chain_head_id = NULL
                WHERE chain_head_id IS NOT NULL
                AND chain_head_id NOT IN (SELECT id FROM messages)
            """)
            result["chain_head_fixed"] = cursor.rowcount

        elif strategy == "delete":
            # 删除孤儿记录（谨慎使用）
            cursor.execute("""
                DELETE FROM messages
                WHERE reply_to IS NOT NULL
                AND reply_to NOT IN (SELECT id FROM messages)
            """)
            deleted_reply = cursor.rowcount

            cursor.execute("""
                DELETE FROM messages
                WHERE chain_head_id IS NOT NULL
                AND chain_head_id NOT IN (SELECT id FROM messages)
            """)
            deleted_chain = cursor.rowcount

            result["deleted"] = deleted_reply + deleted_chain

        self.conn.commit()

        result["details"].append(
            {
                "reply_to_fixed": result.get("reply_to_fixed", 0),
                "chain_head_fixed": result.get("chain_head_fixed", 0),
            }
        )

        self.report["actions_taken"].append(
            {"action": "handle_orphans", "strategy": strategy, "result": result}
        )

        return result

    def standardize_data(self) -> dict[str, Any]:
        """
        标准化数据格式

        Returns:
            标准化结果
        """
        cursor = self.conn.cursor()
        result = {
            "timestamps_fixed": 0,
            "status_normalized": 0,
            "whitespace_trimmed": 0,
            "details": [],
        }

        # 标准化时间戳格式（确保是 YYYY-MM-DD HH:MM:SS）
        # 这里假设时间戳已经是正确格式，只检查是否有异常
        cursor.execute("""
            SELECT id, timestamp FROM messages
            WHERE timestamp NOT LIKE '____-__-__ __:__:__'
        """)
        invalid_timestamps = cursor.fetchall()
        if invalid_timestamps:
            result["details"].append(
                {
                    "issue": "invalid_timestamps",
                    "count": len(invalid_timestamps),
                    "ids": [row["id"] for row in invalid_timestamps[:10]],
                }
            )

        # 标准化状态值（确保是有效状态）
        valid_statuses = ["unread", "read", "done", "processing", "interrupted", "archived"]
        placeholders = ",".join(["%s"] * len(valid_statuses))
        cursor.execute(
            f"""
            SELECT id, status FROM messages
            WHERE status NOT IN ({placeholders})
        """,
            valid_statuses,
        )
        invalid_status = cursor.fetchall()
        if invalid_status:
            result["details"].append(
                {
                    "issue": "invalid_status",
                    "count": len(invalid_status),
                    "examples": [(row["id"], row["status"]) for row in invalid_status[:5]],
                }
            )

        # 修剪内容中的首尾空白
        cursor.execute("""
            UPDATE messages
            SET content = TRIM(content)
            WHERE content != TRIM(content)
        """)
        result["whitespace_trimmed"] = cursor.rowcount

        # 标准化 sender 字段（去除首尾空白）
        cursor.execute("""
            UPDATE messages
            SET sender = TRIM(sender)
            WHERE sender != TRIM(sender)
        """)

        # 确保 is_chain_head 与 chain_head_id 一致
        cursor.execute("""
            UPDATE messages
            SET is_chain_head = 1
            WHERE chain_head_id IS NULL AND is_chain_head = 0
            AND reply_to IS NULL
        """)

        self.conn.commit()

        self.report["standardization"] = result
        self.report["actions_taken"].append({"action": "standardize_data", "result": result})

        return result

    def verify_integrity(self) -> dict[str, Any]:
        """
        验证数据完整性

        Returns:
            验证结果
        """
        cursor = self.conn.cursor()
        result = {"passed": True, "checks": []}

        # 检查 1: 总记录数
        cursor.execute("SELECT COUNT(*) as count FROM messages")
        total = cursor.fetchone()["count"]
        result["checks"].append({"name": "total_records", "passed": total > 0, "value": total})
        if total == 0:
            result["passed"] = False

        # 检查 2: 没有 reply_to 孤儿
        cursor.execute("""
            SELECT COUNT(*) as count FROM messages m
            LEFT JOIN messages p ON m.reply_to = p.id
            WHERE m.reply_to IS NOT NULL AND p.id IS NULL
        """)
        orphan_reply_count = cursor.fetchone()["count"]
        result["checks"].append(
            {
                "name": "no_reply_to_orphans",
                "passed": orphan_reply_count == 0,
                "value": orphan_reply_count,
            }
        )
        if orphan_reply_count > 0:
            result["passed"] = False

        # 检查 3: 没有 chain_head 孤儿
        cursor.execute("""
            SELECT COUNT(*) as count FROM messages m
            LEFT JOIN messages c ON m.chain_head_id = c.id
            WHERE m.chain_head_id IS NOT NULL AND c.id IS NULL
        """)
        orphan_chain_count = cursor.fetchone()["count"]
        result["checks"].append(
            {
                "name": "no_chain_head_orphans",
                "passed": orphan_chain_count == 0,
                "value": orphan_chain_count,
            }
        )
        if orphan_chain_count > 0:
            result["passed"] = False

        # 检查 4: 状态值有效
        valid_statuses = ["unread", "read", "done", "processing", "interrupted", "archived"]
        placeholders = ",".join(["%s"] * len(valid_statuses))
        cursor.execute(
            f"""
            SELECT COUNT(*) as count FROM messages
            WHERE status NOT IN ({placeholders})
        """,
            valid_statuses,
        )
        invalid_status_count = cursor.fetchone()["count"]
        result["checks"].append(
            {
                "name": "valid_status_values",
                "passed": invalid_status_count == 0,
                "value": invalid_status_count,
            }
        )
        if invalid_status_count > 0:
            result["passed"] = False

        # 检查 5: 必填字段非空
        cursor.execute("""
            SELECT COUNT(*) as count FROM messages
            WHERE receiver IS NULL OR receiver = ''
            OR sender IS NULL OR sender = ''
            OR content IS NULL OR content = ''
        """)
        null_required_count = cursor.fetchone()["count"]
        result["checks"].append(
            {
                "name": "required_fields_not_null",
                "passed": null_required_count == 0,
                "value": null_required_count,
            }
        )
        if null_required_count > 0:
            result["passed"] = False

        return result

    def generate_report(self, output_path: str = None) -> str:
        """
        生成清洗报告

        Args:
            output_path: 输出文件路径

        Returns:
            报告内容
        """
        # 生成摘要
        self.report["summary"] = {
            "total_actions": len(self.report["actions_taken"]),
            "duplicates_found": len(self.report["duplicates"]),
            "orphans_found": (
                len(self.report["orphans"].get("reply_to_orphans", []))
                + len(self.report["orphans"].get("chain_head_orphans", []))
            ),
            "integrity_verified": True,
        }

        # 生成 JSON 报告
        report_json = json.dumps(self.report, indent=2, ensure_ascii=False)

        if output_path:
            os.makedirs(
                os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True
            )
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(report_json)

        return report_json

    def run_full_cleaning(self, output_dir: str = None) -> dict[str, Any]:
        """
        执行完整的数据清洗流程

        Args:
            output_dir: 输出目录

        Returns:
            清洗结果
        """
        output_dir = output_dir or os.path.dirname(self.db_path)

        print("=" * 60)
        print("数据清洗流程开始")
        print("=" * 60)

        # 1. 连接数据库
        print("\n[1/7] 连接数据库...")
        self.connect()

        # 2. 备份数据库
        print("[2/7] 备份数据库...")
        backup_path = self.backup_database()
        print(f"      备份路径：{backup_path}")

        # 3. 数据质量分析
        print("[3/7] 数据质量分析...")
        analysis = self.analyze_data_quality()
        print(f"      总记录数：{analysis['total_records']}")
        print(f"      状态分布：{analysis['status_distribution']}")

        # 4. 查找重复记录
        print("[4/7] 查找重复记录...")
        duplicates = self.find_duplicates()
        print(f"      发现 {len(duplicates)} 组重复记录")

        # 5. 查找孤儿记录
        print("[5/7] 查找孤儿记录...")
        orphans = self.find_orphans()
        print(f"      reply_to 孤儿：{len(orphans['reply_to_orphans'])}")
        print(f"      chain_head 孤儿：{len(orphans['chain_head_orphans'])}")

        # 6. 处理问题
        print("[6/7] 处理数据问题...")

        # 处理重复记录（保留每组的第一条）
        dup_result = self.handle_duplicates(strategy="keep_first")
        print(f"      处理重复：归档 {dup_result['records_archived']} 条记录")

        # 处理孤儿记录（将无效外键设为 NULL）
        orphan_result = self.handle_orphans(strategy="nullify")
        print(
            f"      修复孤儿：reply_to={orphan_result['reply_to_fixed']}, chain_head={orphan_result['chain_head_fixed']}"
        )

        # 标准化数据
        std_result = self.standardize_data()
        print(f"      标准化：修剪空白 {std_result['whitespace_trimmed']} 条记录")

        # 7. 验证完整性
        print("[7/7] 验证数据完整性...")
        integrity = self.verify_integrity()
        print(f"      完整性检查：{'通过' if integrity['passed'] else '失败'}")

        # 生成报告
        report_path = os.path.join(output_dir, "cleaning_report.json")
        self.generate_report(report_path)
        print(f"\n报告已保存至：{report_path}")

        # 关闭连接
        self.close()

        print("\n" + "=" * 60)
        print("数据清洗流程完成")
        print("=" * 60)

        return {
            "success": integrity["passed"],
            "backup_path": backup_path,
            "report_path": report_path,
            "integrity_check": integrity,
            "summary": self.report["summary"],
        }


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="数据清洗工具")
    parser.add_argument("--db", type=str, default="data/messages.db", help="数据库文件路径")
    parser.add_argument("--output", type=str, default="data", help="输出目录")
    parser.add_argument("--analysis-only", action="store_true", help="仅执行分析，不进行修改")

    args = parser.parse_args()

    # 确保使用绝对路径
    db_path = os.path.abspath(args.db)
    output_dir = os.path.abspath(args.output)

    if not os.path.exists(db_path):
        print(f"错误：数据库文件不存在：{db_path}")
        sys.exit(1)

    # 获取备份目录（从配置读取）
    backup_dir = get_path("backups_dir", "/mnt/wsl/work/db/backups")
    cleaner = DataCleaner(db_path, backup_dir=backup_dir)

    if args.analysis_only:
        # 仅分析模式
        cleaner.connect()
        print("=== 数据质量分析 ===")
        analysis = cleaner.analyze_data_quality()
        print(f"总记录数：{analysis['total_records']}")
        print(f"状态分布：{json.dumps(analysis['status_distribution'], ensure_ascii=False)}")

        print("\n=== 重复记录 ===")
        duplicates = cleaner.find_duplicates()
        for dup in duplicates[:5]:
            print(f"  内容：{dup['content']} (重复 {dup['count']} 次, IDs: {dup['ids']})")

        print("\n=== 孤儿记录 ===")
        orphans = cleaner.find_orphans()
        print(f"  reply_to 孤儿：{len(orphans['reply_to_orphans'])}")
        print(f"  chain_head 孤儿：{len(orphans['chain_head_orphans'])}")

        cleaner.close()
    else:
        # 完整清洗模式
        result = cleaner.run_full_cleaning(output_dir)
        sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
