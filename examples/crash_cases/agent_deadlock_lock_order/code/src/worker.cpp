void Worker::publish() { lock(cache_mutex_); lock(worker_mutex_); }
