/* SPDX-License-Identifier: MIT */
/*
 * Read-only, headless Blu-ray playlist metadata exporter for bdencode.
 *
 * The program deliberately uses only libbluray's public API plus POSIX
 * directory reads.  Diagnostics go to stderr and stdout contains one JSON
 * document on success.
 */

#define _XOPEN_SOURCE 700

#include <libbluray/bluray-version.h>
#include <libbluray/bluray.h>

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>

#define TICKS_PER_SECOND 90000.0
#define MAX_PLAYLISTS 4096U

typedef struct {
    uint32_t *items;
    size_t count;
    size_t capacity;
} id_list_t;

typedef struct {
    uint32_t playlist;
    uint32_t title_index;
} title_map_entry_t;

typedef struct {
    uint32_t playlist;
    BLURAY_TITLE_INFO *info;
} playlist_entry_t;

typedef struct {
    uint16_t pid;
    uint8_t coding_type;
    uint8_t format;
    uint8_t rate;
    uint8_t char_code;
    uint8_t aspect;
    uint8_t subpath_id;
    char language[4];
    const char *kind;
} stream_record_t;

typedef struct {
    stream_record_t *items;
    size_t count;
    size_t capacity;
} stream_list_t;

static void usage(FILE *stream, const char *program)
{
    fprintf(stream, "Usage: %s --json PATH\n", program);
}

static char *duplicate_slice(const char *value, size_t length)
{
    char *copy;

    if (length == SIZE_MAX) {
        return NULL;
    }
    copy = malloc(length + 1U);
    if (copy == NULL) {
        return NULL;
    }
    memcpy(copy, value, length);
    copy[length] = '\0';
    return copy;
}

static char *path_join(const char *left, const char *right)
{
    const size_t left_length = strlen(left);
    const size_t right_length = strlen(right);
    const bool needs_separator = left_length > 0U && left[left_length - 1U] != '/';
    size_t total;
    char *result;

    if (left_length > SIZE_MAX - right_length - 2U) {
        return NULL;
    }
    total = left_length + right_length + (needs_separator ? 1U : 0U) + 1U;
    result = malloc(total);
    if (result == NULL) {
        return NULL;
    }
    memcpy(result, left, left_length);
    if (needs_separator) {
        result[left_length] = '/';
    }
    memcpy(result + left_length + (needs_separator ? 1U : 0U), right,
           right_length + 1U);
    return result;
}

static bool directory_exists(const char *path)
{
    struct stat status;

    return stat(path, &status) == 0 && S_ISDIR(status.st_mode);
}

static char *disc_root_from_argument(const char *argument)
{
    size_t length = strlen(argument);
    char *trimmed;
    char *playlist_directory;

    while (length > 1U && argument[length - 1U] == '/') {
        --length;
    }
    trimmed = duplicate_slice(argument, length);
    if (trimmed == NULL) {
        return NULL;
    }

    if (length >= 4U && strcasecmp(trimmed + length - 4U, "BDMV") == 0 &&
        (length == 4U || trimmed[length - 5U] == '/')) {
        if (length == 4U) {
            free(trimmed);
            trimmed = duplicate_slice(".", 1U);
        } else {
            size_t root_length = length - 5U;
            while (root_length > 1U && trimmed[root_length - 1U] == '/') {
                --root_length;
            }
            if (root_length == 0U) {
                root_length = 1U;
            }
            {
                char *root = duplicate_slice(trimmed, root_length);
                free(trimmed);
                trimmed = root;
            }
        }
        if (trimmed == NULL) {
            return NULL;
        }
    }

    playlist_directory = path_join(trimmed, "BDMV/PLAYLIST");
    if (playlist_directory == NULL || !directory_exists(trimmed) ||
        !directory_exists(playlist_directory)) {
        fprintf(stderr, "Not a readable Blu-ray directory: %s\n", argument);
        free(playlist_directory);
        free(trimmed);
        return NULL;
    }
    free(playlist_directory);
    return trimmed;
}

static int compare_uint32(const void *left, const void *right)
{
    const uint32_t a = *(const uint32_t *)left;
    const uint32_t b = *(const uint32_t *)right;

    return (a > b) - (a < b);
}

static bool id_list_add(id_list_t *list, uint32_t value)
{
    uint32_t *resized;
    size_t next_capacity;

    if (list->count == list->capacity) {
        next_capacity = list->capacity == 0U ? 64U : list->capacity * 2U;
        if (next_capacity > MAX_PLAYLISTS) {
            next_capacity = MAX_PLAYLISTS;
        }
        if (next_capacity <= list->capacity ||
            next_capacity > SIZE_MAX / sizeof(*list->items)) {
            return false;
        }
        resized = realloc(list->items, next_capacity * sizeof(*list->items));
        if (resized == NULL) {
            return false;
        }
        list->items = resized;
        list->capacity = next_capacity;
    }
    list->items[list->count++] = value;
    return true;
}

static bool parse_playlist_filename(const char *name, uint32_t *playlist)
{
    size_t index;
    unsigned long value;
    char digits[6];
    char *end = NULL;

    if (strlen(name) != 10U || strcasecmp(name + 5U, ".mpls") != 0) {
        return false;
    }
    for (index = 0U; index < 5U; ++index) {
        if (!isdigit((unsigned char)name[index])) {
            return false;
        }
        digits[index] = name[index];
    }
    digits[5] = '\0';
    errno = 0;
    value = strtoul(digits, &end, 10);
    if (errno != 0 || end == digits || *end != '\0' || value > 99999UL) {
        return false;
    }
    *playlist = (uint32_t)value;
    return true;
}

static bool collect_playlist_ids(const char *disc_root, id_list_t *list)
{
    char *playlist_directory = path_join(disc_root, "BDMV/PLAYLIST");
    DIR *directory;
    struct dirent *entry;

    if (playlist_directory == NULL) {
        return false;
    }
    directory = opendir(playlist_directory);
    if (directory == NULL) {
        fprintf(stderr, "Unable to read %s: %s\n", playlist_directory,
                strerror(errno));
        free(playlist_directory);
        return false;
    }
    errno = 0;
    while ((entry = readdir(directory)) != NULL) {
        uint32_t playlist;

        if (!parse_playlist_filename(entry->d_name, &playlist)) {
            continue;
        }
        if (list->count == MAX_PLAYLISTS) {
            fprintf(stderr,
                    "Playlist limit (%u) reached; remaining MPLS files are omitted\n",
                    MAX_PLAYLISTS);
            break;
        }
        if (!id_list_add(list, playlist)) {
            fprintf(stderr, "Out of memory while collecting playlists\n");
            closedir(directory);
            free(playlist_directory);
            return false;
        }
    }
    if (errno != 0) {
        fprintf(stderr, "Unable to finish reading %s: %s\n", playlist_directory,
                strerror(errno));
        closedir(directory);
        free(playlist_directory);
        return false;
    }
    closedir(directory);
    free(playlist_directory);

    qsort(list->items, list->count, sizeof(*list->items), compare_uint32);
    if (list->count > 1U) {
        size_t source;
        size_t destination = 1U;

        for (source = 1U; source < list->count; ++source) {
            if (list->items[source] != list->items[destination - 1U]) {
                list->items[destination++] = list->items[source];
            }
        }
        list->count = destination;
    }
    return true;
}

static size_t utf8_sequence_length(const unsigned char *text)
{
    const unsigned char first = text[0];

    if (first >= 0xc2U && first <= 0xdfU &&
        text[1] >= 0x80U && text[1] <= 0xbfU) {
        return 2U;
    }
    if (first >= 0xe0U && first <= 0xefU && text[1] != '\0' && text[2] != '\0' &&
        text[1] >= 0x80U && text[1] <= 0xbfU &&
        text[2] >= 0x80U && text[2] <= 0xbfU &&
        !(first == 0xe0U && text[1] < 0xa0U) &&
        !(first == 0xedU && text[1] >= 0xa0U)) {
        return 3U;
    }
    if (first >= 0xf0U && first <= 0xf4U && text[1] != '\0' && text[2] != '\0' &&
        text[3] != '\0' && text[1] >= 0x80U && text[1] <= 0xbfU &&
        text[2] >= 0x80U && text[2] <= 0xbfU &&
        text[3] >= 0x80U && text[3] <= 0xbfU &&
        !(first == 0xf0U && text[1] < 0x90U) &&
        !(first == 0xf4U && text[1] > 0x8fU)) {
        return 4U;
    }
    return 0U;
}

static void json_string(const char *value)
{
    const unsigned char *cursor = (const unsigned char *)value;

    putchar('"');
    while (*cursor != '\0') {
        size_t sequence_length;

        switch (*cursor) {
        case '"':
            fputs("\\\"", stdout);
            ++cursor;
            continue;
        case '\\':
            fputs("\\\\", stdout);
            ++cursor;
            continue;
        case '\b':
            fputs("\\b", stdout);
            ++cursor;
            continue;
        case '\f':
            fputs("\\f", stdout);
            ++cursor;
            continue;
        case '\n':
            fputs("\\n", stdout);
            ++cursor;
            continue;
        case '\r':
            fputs("\\r", stdout);
            ++cursor;
            continue;
        case '\t':
            fputs("\\t", stdout);
            ++cursor;
            continue;
        default:
            break;
        }
        if (*cursor < 0x20U) {
            printf("\\u%04x", (unsigned)*cursor);
            ++cursor;
        } else if (*cursor < 0x80U) {
            putchar((int)*cursor++);
        } else {
            sequence_length = utf8_sequence_length(cursor);
            if (sequence_length == 0U) {
                fputs("\\ufffd", stdout);
                ++cursor;
            } else {
                fwrite(cursor, 1U, sequence_length, stdout);
                cursor += sequence_length;
            }
        }
    }
    putchar('"');
}

static void json_nullable_string(const char *value)
{
    if (value == NULL || value[0] == '\0') {
        fputs("null", stdout);
    } else {
        json_string(value);
    }
}

static double seconds_from_ticks(uint64_t ticks)
{
    return (double)ticks / TICKS_PER_SECOND;
}

static const char *stream_kind(uint8_t coding_type)
{
    switch (coding_type) {
    case 0x01: /* MPEG-1 video */
    case 0x02: /* MPEG-2 video */
    case 0x1b: /* AVC */
    case 0x20: /* MVC dependent view */
    case 0x24: /* HEVC */
    case 0xea: /* VC-1 */
        return "video";
    case 0x03: /* MPEG-1 audio */
    case 0x04: /* MPEG-2 audio */
    case 0x80: /* LPCM */
    case 0x81: /* AC-3 */
    case 0x82: /* DTS */
    case 0x83: /* TrueHD */
    case 0x84: /* E-AC-3 */
    case 0x85: /* DTS-HD */
    case 0x86: /* DTS-HD MA */
    case 0xa1: /* secondary E-AC-3 */
    case 0xa2: /* secondary DTS-HD */
        return "audio";
    case 0x90: /* PGS */
    case 0x91: /* IG */
    case 0x92: /* text subtitle */
        return "subtitle";
    default:
        return "unknown";
    }
}

static const char *codec_name(uint8_t coding_type)
{
    switch (coding_type) {
    case 0x01:
        return "mpeg1video";
    case 0x02:
        return "mpeg2video";
    case 0x03:
        return "mp1";
    case 0x04:
        return "mp2";
    case 0x1b:
        return "h264";
    case 0x20:
        return "mvc";
    case 0x24:
        return "hevc";
    case 0x80:
        return "pcm_bluray";
    case 0x81:
        return "ac3";
    case 0x82:
        return "dts";
    case 0x83:
        return "truehd";
    case 0x84:
        return "eac3";
    case 0x85:
        return "dts-hd";
    case 0x86:
        return "dts-hd ma";
    case 0x90:
        return "hdmv_pgs_subtitle";
    case 0x91:
        return "hdmv_ig";
    case 0x92:
        return "textst";
    case 0xa1:
        return "eac3-secondary";
    case 0xa2:
        return "dts-hd-secondary";
    case 0xea:
        return "vc1";
    default:
        return "unknown";
    }
}

static void language_from_info(const BLURAY_STREAM_INFO *stream, char output[4])
{
    size_t index;

    output[0] = '\0';
    for (index = 0U; index < 3U; ++index) {
        const unsigned char value = stream->lang[index];

        if (!((value >= (unsigned char)'A' && value <= (unsigned char)'Z') ||
              (value >= (unsigned char)'a' && value <= (unsigned char)'z'))) {
            output[0] = '\0';
            return;
        }
        output[index] = value >= (unsigned char)'A' && value <= (unsigned char)'Z'
                            ? (char)(value + ((unsigned char)'a' - (unsigned char)'A'))
                            : (char)value;
    }
    output[3] = '\0';
}

static void video_dimensions(uint8_t format, unsigned *width, unsigned *height,
                             const char **field_order)
{
    *width = 0U;
    *height = 0U;
    *field_order = "unknown";
    switch (format) {
    case 1: /* 480i */
        *width = 720U;
        *height = 480U;
        *field_order = "interlaced";
        break;
    case 2: /* 576i */
        *width = 720U;
        *height = 576U;
        *field_order = "interlaced";
        break;
    case 3: /* 480p */
        *width = 720U;
        *height = 480U;
        *field_order = "progressive";
        break;
    case 4: /* 1080i */
        *width = 1920U;
        *height = 1080U;
        *field_order = "interlaced";
        break;
    case 5: /* 720p */
        *width = 1280U;
        *height = 720U;
        *field_order = "progressive";
        break;
    case 6: /* 1080p */
        *width = 1920U;
        *height = 1080U;
        *field_order = "progressive";
        break;
    case 7: /* 576p */
        *width = 720U;
        *height = 576U;
        *field_order = "progressive";
        break;
    case 8: /* 2160p */
        *width = 3840U;
        *height = 2160U;
        *field_order = "progressive";
        break;
    default:
        break;
    }
}

static const char *frame_rate(uint8_t rate)
{
    switch (rate) {
    case 1:
        return "24000/1001";
    case 2:
        return "24/1";
    case 3:
        return "25/1";
    case 4:
        return "30000/1001";
    case 6:
        return "50/1";
    case 7:
        return "60000/1001";
    default:
        return NULL;
    }
}

static unsigned audio_sample_rate(uint8_t rate)
{
    switch (rate) {
    case 1:
        return 48000U;
    case 4:
    case 14:
        return 96000U;
    case 5:
    case 12:
        return 192000U;
    default:
        return 0U;
    }
}

static unsigned audio_channels(uint8_t format)
{
    switch (format) {
    case 1:
        return 1U;
    case 3:
        return 2U;
    default:
        return 0U;
    }
}

static stream_record_t make_stream_record(const BLURAY_STREAM_INFO *stream,
                                          const char *kind)
{
    stream_record_t record;

    memset(&record, 0, sizeof(record));
    record.pid = stream->pid;
    record.coding_type = stream->coding_type;
    record.format = stream->format;
    record.rate = stream->rate;
    record.char_code = stream->char_code;
    record.aspect = stream->aspect;
    record.subpath_id = stream->subpath_id;
    record.kind = kind;
    language_from_info(stream, record.language);
    return record;
}

static bool stream_list_add(stream_list_t *list, const BLURAY_STREAM_INFO *stream,
                            const char *kind)
{
    stream_record_t record = make_stream_record(stream, kind);
    size_t index;
    stream_record_t *resized;
    size_t next_capacity;

    for (index = 0U; index < list->count; ++index) {
        if (list->items[index].pid == record.pid &&
            list->items[index].coding_type == record.coding_type &&
            strcmp(list->items[index].kind, record.kind) == 0) {
            if (list->items[index].language[0] == '\0' && record.language[0] != '\0') {
                memcpy(list->items[index].language, record.language,
                       sizeof(record.language));
            }
            return true;
        }
    }
    if (list->count == list->capacity) {
        next_capacity = list->capacity == 0U ? 16U : list->capacity * 2U;
        if (next_capacity <= list->capacity ||
            next_capacity > SIZE_MAX / sizeof(*list->items)) {
            return false;
        }
        resized = realloc(list->items, next_capacity * sizeof(*list->items));
        if (resized == NULL) {
            return false;
        }
        list->items = resized;
        list->capacity = next_capacity;
    }
    list->items[list->count++] = record;
    return true;
}

static bool collect_clip_streams(stream_list_t *list, const BLURAY_CLIP_INFO *clip)
{
    size_t index;

#define ADD_STREAM_ARRAY(member, count_member, kind_name)                            \
    do {                                                                              \
        for (index = 0U; index < (size_t)clip->count_member; ++index) {               \
            if (!stream_list_add(list, &clip->member[index], kind_name)) {            \
                return false;                                                         \
            }                                                                         \
        }                                                                             \
    } while (0)

    ADD_STREAM_ARRAY(video_streams, video_stream_count, "video");
    ADD_STREAM_ARRAY(audio_streams, audio_stream_count, "audio");
    ADD_STREAM_ARRAY(pg_streams, pg_stream_count, "subtitle");
    ADD_STREAM_ARRAY(ig_streams, ig_stream_count, "subtitle");
    ADD_STREAM_ARRAY(sec_video_streams, sec_video_stream_count, "video");
    ADD_STREAM_ARRAY(sec_audio_streams, sec_audio_stream_count, "audio");

#undef ADD_STREAM_ARRAY
    return true;
}

static void print_stream(const stream_record_t *stream)
{
    const char *rate;
    unsigned width;
    unsigned height;
    unsigned sample_rate;
    unsigned channels;
    const char *field_order;

    printf("{\"pid\":%u,\"pid_hex\":\"0x%04x\",\"coding_type\":%u,"
           "\"coding_type_hex\":\"0x%02x\",\"codec_type\":",
           (unsigned)stream->pid, (unsigned)stream->pid,
           (unsigned)stream->coding_type, (unsigned)stream->coding_type);
    json_string(stream->kind != NULL ? stream->kind : stream_kind(stream->coding_type));
    fputs(",\"type\":", stdout);
    json_string(stream->kind != NULL ? stream->kind : stream_kind(stream->coding_type));
    fputs(",\"codec\":", stdout);
    json_string(codec_name(stream->coding_type));
    fputs(",\"codec_name\":", stdout);
    json_string(codec_name(stream->coding_type));
    fputs(",\"mpls_language\":", stdout);
    json_nullable_string(stream->language);
    printf(",\"format_code\":%u,\"rate_code\":%u,\"subpath_id\":%u",
           (unsigned)stream->format, (unsigned)stream->rate,
           (unsigned)stream->subpath_id);

    if (strcmp(stream->kind, "video") == 0) {
        video_dimensions(stream->format, &width, &height, &field_order);
        if (width != 0U) {
            printf(",\"width\":%u,\"height\":%u", width, height);
        }
        fputs(",\"field_order\":", stdout);
        json_string(field_order);
        rate = frame_rate(stream->rate);
        if (rate != NULL) {
            fputs(",\"frame_rate\":", stdout);
            json_string(rate);
        }
        if (stream->aspect == 2U || stream->aspect == 3U) {
            fputs(",\"aspect_ratio\":", stdout);
            json_string(stream->aspect == 2U ? "4:3" : "16:9");
        }
    } else if (strcmp(stream->kind, "audio") == 0) {
        sample_rate = audio_sample_rate(stream->rate);
        channels = audio_channels(stream->format);
        if (sample_rate != 0U) {
            printf(",\"sample_rate\":%u", sample_rate);
        }
        if (channels != 0U) {
            printf(",\"channels\":%u", channels);
            fputs(",\"channel_layout\":", stdout);
            json_string(channels == 1U ? "mono" : "stereo");
        }
    } else if (strcmp(stream->kind, "subtitle") == 0 && stream->char_code != 0U) {
        printf(",\"char_code\":%u", (unsigned)stream->char_code);
    }
    putchar('}');
}

static void print_stream_array(const BLURAY_STREAM_INFO *streams, size_t count,
                               const char *kind, bool *first)
{
    size_t index;

    for (index = 0U; index < count; ++index) {
        stream_record_t record = make_stream_record(&streams[index], kind);

        if (!*first) {
            putchar(',');
        }
        *first = false;
        print_stream(&record);
    }
}

static void print_clip_streams(const BLURAY_CLIP_INFO *clip)
{
    bool first = true;

    putchar('[');
    print_stream_array(clip->video_streams, clip->video_stream_count, "video", &first);
    print_stream_array(clip->audio_streams, clip->audio_stream_count, "audio", &first);
    print_stream_array(clip->pg_streams, clip->pg_stream_count, "subtitle", &first);
    print_stream_array(clip->ig_streams, clip->ig_stream_count, "subtitle", &first);
    print_stream_array(clip->sec_video_streams, clip->sec_video_stream_count,
                       "video", &first);
    print_stream_array(clip->sec_audio_streams, clip->sec_audio_stream_count,
                       "audio", &first);
    putchar(']');
}

static void print_chapters(const BLURAY_TITLE_INFO *info)
{
    uint32_t index;

    putchar('[');
    for (index = 0U; index < info->chapter_count; ++index) {
        const BLURAY_TITLE_CHAPTER *chapter = &info->chapters[index];

        if (index != 0U) {
            putchar(',');
        }
        printf("{\"index\":%" PRIu32 ",\"start_time\":%.6f,"
               "\"duration\":%.6f,\"start_ticks\":%" PRIu64 ","
               "\"duration_ticks\":%" PRIu64 ",\"clip_ref\":%u}",
               chapter->idx, seconds_from_ticks(chapter->start),
               seconds_from_ticks(chapter->duration), chapter->start,
               chapter->duration, chapter->clip_ref);
    }
    putchar(']');
}

static bool build_union_streams(const BLURAY_TITLE_INFO *info, stream_list_t *streams)
{
    uint32_t index;

    for (index = 0U; index < info->clip_count; ++index) {
        if (!collect_clip_streams(streams, &info->clips[index])) {
            fprintf(stderr, "Out of memory while collecting playlist streams\n");
            return false;
        }
    }
    return true;
}

static void print_segments(const BLURAY_TITLE_INFO *info)
{
    uint32_t index;

    putchar('[');
    for (index = 0U; index < info->clip_count; ++index) {
        const BLURAY_CLIP_INFO *clip = &info->clips[index];
        char clip_id[7];

        memcpy(clip_id, clip->clip_id, 6U);
        clip_id[6] = '\0';
        if (index != 0U) {
            putchar(',');
        }
        fputs("{\"clip_id\":", stdout);
        json_string(clip_id);
        printf(",\"in_time\":%.6f,\"out_time\":%.6f,"
               "\"relative_start\":%.6f,\"duration\":%.6f,"
               "\"in_time_ticks\":%" PRIu64 ",\"out_time_ticks\":%" PRIu64 ","
               "\"relative_start_ticks\":%" PRIu64 ",\"packet_count\":%" PRIu32 ","
               "\"angle\":1,\"streams\":",
               seconds_from_ticks(clip->in_time), seconds_from_ticks(clip->out_time),
               seconds_from_ticks(clip->start_time),
               seconds_from_ticks(clip->out_time - clip->in_time), clip->in_time,
               clip->out_time, clip->start_time, clip->pkt_count);
        print_clip_streams(clip);
        putchar('}');
    }
    putchar(']');
}

static void print_title_indices(uint32_t playlist, const title_map_entry_t *title_map,
                                size_t title_count)
{
    size_t index;
    bool first = true;

    putchar('[');
    for (index = 0U; index < title_count; ++index) {
        if (title_map[index].playlist != playlist) {
            continue;
        }
        if (!first) {
            putchar(',');
        }
        first = false;
        printf("%" PRIu32, title_map[index].title_index);
    }
    putchar(']');
}

static bool playlist_is_main(uint32_t playlist, int main_title,
                             const title_map_entry_t *title_map, size_t title_count)
{
    size_t index;

    if (main_title < 0) {
        return false;
    }
    for (index = 0U; index < title_count; ++index) {
        if (title_map[index].title_index == (uint32_t)main_title) {
            return title_map[index].playlist == playlist;
        }
    }
    return false;
}

static bool print_playlist(const playlist_entry_t *playlist,
                           const title_map_entry_t *title_map, size_t title_count,
                           int main_title)
{
    const BLURAY_TITLE_INFO *info = playlist->info;
    stream_list_t streams = {0};
    size_t index;

    if (!build_union_streams(info, &streams)) {
        free(streams.items);
        return false;
    }
    printf("{\"id\":\"%05" PRIu32 "\",\"playlist_id\":\"%05" PRIu32
           "\",\"title_indices\":",
           playlist->playlist, playlist->playlist);
    print_title_indices(playlist->playlist, title_map, title_count);
    printf(",\"duration\":%.6f,\"duration_ticks\":%" PRIu64 ","
           "\"angle_count\":%u,\"selected_angle\":1,"
           "\"chapter_count\":%" PRIu32 ",\"chapters\":",
           seconds_from_ticks(info->duration), info->duration,
           (unsigned)info->angle_count, info->chapter_count);
    print_chapters(info);
    printf(",\"clip_count\":%" PRIu32 ",\"segments\":", info->clip_count);
    print_segments(info);
    fputs(",\"streams\":[", stdout);
    for (index = 0U; index < streams.count; ++index) {
        if (index != 0U) {
            putchar(',');
        }
        print_stream(&streams.items[index]);
    }
    printf("],\"recommended\":%s}",
           playlist_is_main(playlist->playlist, main_title, title_map, title_count)
               ? "true"
               : "false");
    free(streams.items);
    return true;
}

static title_map_entry_t *build_title_map(BLURAY *disc, uint32_t title_count)
{
    title_map_entry_t *map;
    uint32_t index;

    if (title_count == 0U) {
        return NULL;
    }
    map = calloc(title_count, sizeof(*map));
    if (map == NULL) {
        return NULL;
    }
    for (index = 0U; index < title_count; ++index) {
        BLURAY_TITLE_INFO *info = bd_get_title_info(disc, index, 0U);

        map[index].title_index = index;
        map[index].playlist = UINT32_MAX;
        if (info != NULL) {
            map[index].playlist = info->playlist;
            bd_free_title_info(info);
        }
    }
    return map;
}

static void print_disc_info(const BLURAY_DISC_INFO *info)
{
    fputs("{\"bluray_detected\":", stdout);
    fputs(info != NULL && info->bluray_detected ? "true" : "false", stdout);
    fputs(",\"disc_name\":", stdout);
    json_nullable_string(info != NULL ? info->disc_name : NULL);
    fputs(",\"udf_volume_id\":", stdout);
    json_nullable_string(info != NULL ? info->udf_volume_id : NULL);
    printf(",\"content_exists_3d\":%s",
           info != NULL && info->content_exist_3D ? "true" : "false");
#if defined(BLURAY_VERSION) && BLURAY_VERSION >= BLURAY_VERSION_CODE(1, 2, 1)
    if (info != NULL) {
        printf(",\"initial_dynamic_range_type\":%u",
               (unsigned)info->initial_dynamic_range_type);
    }
#endif
    putchar('}');
}

static int export_json(const char *argument)
{
    char *disc_root = disc_root_from_argument(argument);
    BLURAY *disc;
    const BLURAY_DISC_INFO *disc_info;
    id_list_t ids = {0};
    playlist_entry_t *playlists = NULL;
    title_map_entry_t *title_map = NULL;
    uint32_t title_count;
    int main_title;
    size_t playlist_count = 0U;
    size_t index;
    int result = EXIT_FAILURE;

    if (disc_root == NULL) {
        return EXIT_FAILURE;
    }
    if (!collect_playlist_ids(disc_root, &ids)) {
        goto cleanup;
    }
    if (ids.count == 0U) {
        fprintf(stderr, "No five-digit MPLS files found under %s\n", disc_root);
        goto cleanup;
    }

    disc = bd_open(disc_root, NULL);
    if (disc == NULL) {
        fprintf(stderr, "libbluray could not open disc: %s\n", disc_root);
        goto cleanup;
    }
    disc_info = bd_get_disc_info(disc);
    title_count = bd_get_titles(disc, TITLES_ALL, 0U);
    main_title = title_count > 0U ? bd_get_main_title(disc) : -1;
    title_map = build_title_map(disc, title_count);
    if (title_count > 0U && title_map == NULL) {
        fprintf(stderr, "Out of memory while collecting title metadata\n");
        bd_close(disc);
        goto cleanup;
    }

    playlists = calloc(ids.count, sizeof(*playlists));
    if (playlists == NULL) {
        fprintf(stderr, "Out of memory while collecting playlist metadata\n");
        bd_close(disc);
        goto cleanup;
    }
    for (index = 0U; index < ids.count; ++index) {
        BLURAY_TITLE_INFO *info = bd_get_playlist_info(disc, ids.items[index], 0U);

        if (info == NULL) {
            fprintf(stderr, "libbluray could not parse playlist %05" PRIu32 "\n",
                    ids.items[index]);
            continue;
        }
        playlists[playlist_count].playlist = ids.items[index];
        playlists[playlist_count].info = info;
        ++playlist_count;
    }
    if (playlist_count == 0U) {
        fprintf(stderr, "libbluray did not return any readable playlists\n");
        bd_close(disc);
        goto cleanup;
    }

    fputs("{\"schema_version\":1,\"libbluray_version\":", stdout);
    json_string(BLURAY_VERSION_STRING);
    fputs(",\"source\":", stdout);
    json_string(disc_root);
    fputs(",\"disc\":", stdout);
    print_disc_info(disc_info);
    printf(",\"title_count\":%" PRIu32 ",\"main_title_index\":", title_count);
    if (main_title < 0) {
        fputs("null", stdout);
    } else {
        printf("%d", main_title);
    }
    fputs(",\"playlists\":[", stdout);
    for (index = 0U; index < playlist_count; ++index) {
        if (index != 0U) {
            putchar(',');
        }
        if (!print_playlist(&playlists[index], title_map, title_count, main_title)) {
            fputs("null", stdout);
        }
    }
    fputs("]}\n", stdout);
    if (ferror(stdout)) {
        fprintf(stderr, "Unable to write JSON output\n");
    } else {
        result = EXIT_SUCCESS;
    }

    for (index = 0U; index < playlist_count; ++index) {
        bd_free_title_info(playlists[index].info);
    }
    bd_close(disc);

cleanup:
    free(playlists);
    free(title_map);
    free(ids.items);
    free(disc_root);
    return result;
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "--help") == 0) {
        usage(stdout, argv[0]);
        return EXIT_SUCCESS;
    }
    if (argc != 3 || strcmp(argv[1], "--json") != 0 || argv[2][0] == '\0') {
        usage(stderr, argv[0]);
        return 2;
    }
    return export_json(argv[2]);
}
