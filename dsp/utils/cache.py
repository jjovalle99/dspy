import os
import sqlite3
import sys
import time
from timeit import default_timer
import traceback
from typing import Any, Callable, Optional, cast, Dict, List
from functools import wraps
import uuid
from datetime import datetime
import hashlib
import threading
from dsp.utils.logger import get_logger
from collections import OrderedDict
import pickle
import functools


logger = get_logger(logging_level=int(os.getenv("DSP_LOGGING_LEVEL", "20")))

MAX_POLL_TIME = 10
POLL_INTERVAL = 0.003


def filter_keys(
    input_dict: Dict[str, Any], keys_to_ignore: List[str]
) -> Dict[str, Any]:
    return {
        key: value for key, value in input_dict.items() if key not in keys_to_ignore
    }


def _hash(
    func: Callable[..., Any], *args: Dict[str, Any], **kwargs: Dict[str, Any]
) -> str:
    func_name = func.__name__
    # TODO - check if should add a condition to exclude for lambdas

    # sort the kwargs to ensure consistent hash
    sorted_kwargs = OrderedDict(sorted(kwargs.items(), key=lambda x: x[0]))

    # Convert args and kwargs to strings
    args_str = ','.join(str(arg) for arg in args)
    kwargs_str = ','.join(f'{key}={value}' for key, value in sorted_kwargs.items())

    # Concatenate the function_name, args_str, and kwargs_str
    combined_str = f'{func_name}({args_str},{kwargs_str})'

    # Generate SHA-256 hash
    func_hash = hashlib.sha256(combined_str.encode()).hexdigest()

    return func_hash


class SQLiteCache:
    conn: sqlite3.Connection
    lock: threading.Lock

    def __init__(self):
        """Initialise a SQLite database using the environment key DSP_CACHE_SQLITE_PATH."""
        self.lock = threading.Lock()
        self.conn = None
        self.cursor = None

    def __enter__(self):
        cache_file_path = os.getenv("DSP_CACHE_SQLITE_PATH") or "sqlite_cache.db"
        # Initialize the SQLite connection
        self.conn = sqlite3.connect(
            cache_file_path,
            check_same_thread=False,
            isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        self.create_table_if_not_exists()

    def __exit__(self):
        self.conn.close()


    def create_table_if_not_exists(self):
        """Create a cache table if it does not exist.
        : row_idx: a unique id for each row
        : branch_idx: a unique id for each branch; alias for version
        : operation_hash: a hash of the function name, args, and kwargs
        : insert_timestamp: the time when the row was inserted
        : timestamp: the time when the operation was started
        : status: the status of the operation (FAILED, PENDING, COMPLETED)
        : payload: the payload of the operation
        : result: the result of the operation as a pickled object
        """
        with self.lock:
            with self.conn:
                cursor = self.conn.cursor()
                try:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS cache (
                            row_idx TEXT PRIMARY KEY,
                            branch_idx INTEGER,
                            operation_hash TEXT,
                            insert_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            timestamp FLOAT,
                            status TEXT,
                            payload TEXT,
                            result BLOB
                        )
                        """
                    )
                    self.conn.commit()
                except Exception as e:
                    logger.warn(f"Could not create cache table with exception: {e}")
                    raise e
                finally:
                    cursor.close()
            

    def insert_operation_started(
        self, operation_hash: str, branch_idx: int, timestamp: float
    ) -> str:
        """Insert a row into the table with the status "PENDING"."""
        
        row_idx = str(uuid.uuid4())
        with self.conn:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO cache (row_idx, branch_idx, operation_hash, timestamp, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (row_idx, branch_idx, operation_hash, timestamp, 1),
                )
                self.conn.commit()
            finally:
                cursor.close()
        return row_idx

    def update_operation_status(
        self,
        operation_hash: str,
        branch_idx: int,
        result: Any,
        start_time: float,
        end_time: float,
        row_idx: Optional[str] = None,
        status: int = 2,
    ):
        """update an existing operation record with new status and result fields."""
        
        result = pickle.dump(result)
        
        if row_idx is None:
            sql_query = """
            UPDATE cache
            SET status = ?, result = ?
            WHERE operation_hash = ? AND branch_idx = ? AND status = ? AND timestamp >= ?"""
            sql_inputs = (status, result, operation_hash, branch_idx, 1, start_time)
            if end_time != float("inf"):
                sql_query += " AND timestamp <= ?"
        else:
            sql_query = """
            UPDATE cache
            SET status = ?, result = ?
            WHERE row_idx = ?"""
            sql_inputs = (status, result, row_idx)
        with self.conn:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    sql_query,
                    sql_inputs,
                )
                self.conn.commit()
            finally:
                cursor.close()

    # TODO: Pass status as list. If checking for status 2, also check for status 0, if end_time < curr_timestamp
    def check_if_record_exists_with_status(
        self,
        operation_hash: str,
        branch_idx: int,
        start_time: float,
        end_time: float,
        status: str,
    ) -> bool:
        """Check if a row with the specific status and timerange exists for the given (operation_hash, branch_idx, status)."""
        
        sql_query = """
        SELECT COUNT(*) FROM cache
        WHERE operation_hash = ? AND branch_idx = ? AND status = ? AND timestamp >= ?
        """
        sql_inputs = (operation_hash, branch_idx, status, start_time)
        if end_time != float("inf"):
            sql_query += " AND timestamp <= ?"
            sql_inputs += (end_time,)

        sql_query += " ORDER BY timestamp ASC LIMIT 1"
        with self.conn:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    sql_query,
                    sql_inputs,
                )
                return cursor.fetchone()[0] > 0
            finally:
                cursor.close()

    def retrieve_earliest_record(
        self,
        function_hash: str,
        cache_branch: int,
        range_start_timestamp: float,
        range_end_timestamp: float,
        retrieve_status: Optional[str] = None,
        return_record_result: bool = False,
    ):
        """Retrieve the earliest record and use the status priority:
        "FINISHED" > "PENDING" > "FAILED".
        Filter by the range_start_timestamp and range_end_timestamp.
        Returns a tuple of (cache_exists: bool, insertion_timestamp: float)
        """
        
        
        sql_query = """
        SELECT row_idx, insert_timestamp, status, result FROM cache
        WHERE operation_hash LIKE ? AND branch_idx = ? AND timestamp >= ?
        """
        sql_inputs = (function_hash, cache_branch, range_start_timestamp)
        if range_end_timestamp != float("inf"):
            sql_query += " AND timestamp <= ?"
            sql_inputs += (range_end_timestamp,)
        if retrieve_status is not None:
            sql_query += " AND status = ?"
            sql_inputs += (retrieve_status,)
        sql_query += " ORDER BY status DESC, insert_timestamp ASC LIMIT 1"
        with self.conn:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    sql_query,
                    sql_inputs,
                )
                row = cursor.fetchone()
                if row is None:
                    return False, None
                if return_record_result:
                    logger.debug(f"returning record: {row[0]}")
                    return True, pickle.load(row[3])
                return True, None
            finally:
                cursor.close()

    # TODO: Not used?
    def get_cache_record(
        self, operation_hash: str, branch_idx: int, start_time: float, end_time: float
    ):
        """Retrieve the cache record for the given operation_hash."""
        
        sql_query = """
        SELECT result FROM cache
        WHERE operation_hash = ? AND branch_idx = ? AND timestamp >= ?
        """
        sql_inputs = (operation_hash, branch_idx, start_time)
        if end_time != float("inf"):
            sql_query += " AND timestamp <= ?"
            sql_inputs += (end_time,)
        with self.conn:
            cursor = self.conn.cursor()
            try:
                cursor.execute(sql_query, sql_inputs)
                row = cursor.fetchone()
                if row is None:
                    return None
                else:
                    return pickle.load(row[0])
            finally:
                cursor.close()


def sqlite_cache_wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
    """The cache wrapper for the function. This is the function that is called when the user calls the function."""

    @functools.wraps(func)
    def wrapper(*args: Dict[str, Any], **kwargs: Dict[str, Any]) -> Any:
        """The wrapper function uses the arguments worker_id, cache_branch, cache_start_timerange, cache_end_timerange to compute the operation_hash.
        1. It then checks if the operation_hash exists in the cache within the timerange. If it does, it returns the result.
        2. If it does not exists within the timerange, it throws an exception.
        3. If it does exists but the status is "FAILED", it throws the same exception as the reason why it failed.
        4. If it does exists but the status is "PENDING", it polls the cache for MAX_POLL_TIME seconds. If it does not get a result within that time, it throws an exception.
        5. If it does exists but the status is "COMPLETED", it returns the result.
        6. If the cache does not exists within the timerange and the end_timerange is the future, it recomputes and inserts the operation_hash with the status "PENDING" and returns the result.
        """
        cache_client = SQLiteCache()

        request_time = datetime.now().timestamp()
        cache_branch = cast(int, kwargs.get("cache_branch", 0))
        start_time: float = cast(float, kwargs.get("experiment_start_timestamp", 0))
        end_time: float = cast(
            float, kwargs.get("experiment_end_timestamp", float("inf"))
        )
        timerange_in_iso: str = f"{datetime.fromtimestamp(start_time).isoformat()} and {datetime.fromtimestamp(end_time).isoformat() if end_time != float('inf') else 'future'}"

        # remove from kwargs so that don't get passed to the function & don't get hashed
        kwargs = filter_keys(
            kwargs,
            [
                "worker_id",
                "cache_branch",
                "experiment_start_timestamp",
                "experiment_end_timestamp",
            ],
        )

        # get a consistent hash for the function call
        function_hash = _hash(func, *args, **kwargs)

        # check if the cache exists
        if cache_client.check_if_record_exists_with_status(
            function_hash, cache_branch, start_time, end_time, 2 # check for 2 or 0
        ):
            logger.debug(
                f"Cached experiment result found between {timerange_in_iso}. Retrieving result from cache."
            )
            _, result = cache_client.retrieve_earliest_record(
                function_hash,
                cache_branch,
                start_time,
                end_time,
                retrieve_status=2,
                return_record_result=True,
            )
            return result

        # check if the cache is pending
        if cache_client.check_if_record_exists_with_status(
            function_hash, cache_branch, start_time, end_time, 1
        ):
            logger.debug("Operation is pending in the cache. Polling for result.")
            polling_start_time = default_timer()
            while default_timer() - polling_start_time < MAX_POLL_TIME:
                if cache_client.check_if_record_exists_with_status(
                    function_hash, cache_branch, start_time, end_time, 2
                ):
                    logger.debug("Result found after polling. Retrieving from cache.")
                    _, result = cache_client.retrieve_earliest_record(
                        function_hash,
                        cache_branch,
                        start_time,
                        end_time,
                        retrieve_status=2,
                        return_record_result=True,
                    )
                    return result
                time.sleep(POLL_INTERVAL)
            raise Exception("Failed to retrieve result from cache after polling.")

        # check if the cache failed
        if cache_client.check_if_record_exists_with_status(
            function_hash, cache_branch, start_time, end_time, 0
        ):
            if end_time < request_time: # TODO: Move into check_if_record_exists_with_status 
                _, result = cache_client.retrieve_earliest_record(
                    function_hash,
                    cache_branch,
                    start_time,
                    end_time,
                    retrieve_status=0,
                    return_record_result=True,
                )
                logger.debug(
                    f"Failed operation found in the cache for experiment timerange between {timerange_in_iso}. Raising the same exception."
                )
                raise Exception(result)

        if end_time < request_time:
            raise Exception(
                f"Cache does not exist for the given experiment timerange of between {timerange_in_iso}."
            )

        # insert the operation as pending
        row_idx = cache_client.insert_operation_started(
            function_hash, cache_branch, request_time
        )

        # TODO: Add in another polling check to see if another thread started earlier than this one
        # Check for earliest pending, then wait for it. 

        logger.debug(
            f"Could not find a succesful experiment result in the cache for timerange between {timerange_in_iso}. Computing!"
        )
        try:    
            result = func(*args, **kwargs)
        except Exception as exc: # TODO
            result = "\n".join(traceback.format_exception(*sys.exc_info()))
            # update the cache as completed
            cache_client.update_operation_status(
                function_hash,
                cache_branch,
                result,
                start_time,
                end_time,
                row_idx=row_idx,
                status=0,
            )
            raise exc
        # update the cache as completed
        cache_client.update_operation_status(
            function_hash,
            cache_branch,
            result,
            start_time,
            end_time,
            row_idx=row_idx,
            status=2,
        )
        # redundant reads to handle concurrency issues
        _, result = cache_client.retrieve_earliest_record(
            function_hash,
            cache_branch,
            start_time,
            end_time,
            retrieve_status=2,
            return_record_result=True,
        )
        return result

    return wrapper


