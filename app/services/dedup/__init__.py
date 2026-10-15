"""Заглушка, реализация — Фаза 3 (ТЗ §2.4.3, дедупликация).

Визуальная дедупликация по pHash — на Фазе 2 используется только точная
по file_unique_id (см. app/repositories/posts.find_by_file_unique_id
и app/repositories/duplicates._hamming_hex).

На Фазе 3 сюда переедет:
  - вычисление pHash для входящих постов (через imagehash lib)
  - поиск визуальных дубликатов через find_phash_near
  - маркировка как possible_duplicate вместо pending.
"""
