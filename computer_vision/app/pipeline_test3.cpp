/*
 * Based on deepstream-test3 with analytics probe and Redis FPS health.
 */

#include <gst/gst.h>
#include <glib.h>
#include <stdio.h>
#include <math.h>
#include <string.h>
#include <sys/time.h>
#include <cuda_runtime_api.h>
#include <hiredis/hiredis.h>
#include <time.h>
#include <string>
#include <sstream>

#include "gstnvdsmeta.h"
#include "gstnvdsinfer.h"
#include "nvds_yml_parser.h"
#include "gst-nvmessage.h"
#include "nvds_analytics_meta.h"
#include "snapshot_sender.h"

#include <vector>
#include <netdb.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>

#define FACE_RECEIVER_HOST "face-receiver"
#define FACE_RECEIVER_PORT 12348
#define FACE_END_MARKER "END!"
#define FACE_LANDMARKS_DIM 212
#define FACE_EMBEDDING_DIM 512
#define FACE_KPS_COUNT 5
#define FACE_MIN_BBOX_PX   30
#define FACE_MAX_FPS       10

#pragma pack(push, 1)
struct FaceCropPacket {
    uint32_t device_id;
    uint64_t object_id;
    float    quality_score;
    float    bbox_left;
    float    bbox_top;
    float    bbox_width;
    float    bbox_height;
    uint64_t timestamp_ms;
    float    kps[FACE_KPS_COUNT * 2];
};
#pragma pack(pop)

struct FaceRegion { int start; int end; int min_pts; };
static const FaceRegion KEY_REGIONS[4] = {
    {68, 75, 1},
    {76, 83, 1},
    {53, 67, 1},
    {84, 95, 2},
};

/*
 * Utility: check if a YAML config section exists by reading the file.
 * Used to conditionally enable SGIE elements.
 */
static gboolean yaml_has_section(const gchar* file, const gchar* section) {
    gchar key[256];
    g_snprintf(key, sizeof(key), "%s:", section);
    FILE* fp = fopen(file, "r");
    if (!fp) return FALSE;
    gchar line[512];
    gboolean found = FALSE;
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, key, strlen(key)) == 0) { found = TRUE; break; }
    }
    fclose(fp);
    return found;
}

static int yaml_read_int(const gchar* file, const gchar* key, int default_val)
{
    gchar search[256];
    g_snprintf(search, sizeof(search), "%s:", key);
    FILE* fp = fopen(file, "r");
    if (!fp) return default_val;
    gchar line[512];
    int result = default_val;
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, search, strlen(search)) == 0) {
            result = atoi(line + strlen(search));
            break;
        }
    }
    fclose(fp);
    return result;
}

static void yaml_read_labels(const gchar* file, std::vector<std::string>& out)
{
    FILE* fp = fopen(file, "r");
    if (!fp) return;
    gchar line[8192];
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, "labels:", 7) != 0) continue;
        gchar* val = line + 7;
        while (*val == ' ') val++;
        gchar* end = val + strlen(val) - 1;
        while (end > val && (*end == '\n' || *end == '\r')) end--;
        *(end + 1) = '\0';
        gchar* tok = strtok(val, ";");
        while (tok) {
            out.push_back(std::string(tok));
            tok = strtok(NULL, ";");
        }
        break;
    }
    fclose(fp);
}

#define MAX_DISPLAY_LEN 64
#define PGIE_CLASS_ID_VEHICLE 0
#define PGIE_CLASS_ID_PERSON 2
#define OSD_PROCESS_MODE 1
#define OSD_DISPLAY_TEXT 1
#define MUXER_OUTPUT_WIDTH 1920
#define MUXER_OUTPUT_HEIGHT 1080
#define MUXER_BATCH_TIMEOUT_USEC 40000
#define TILED_OUTPUT_WIDTH 1280
#define TILED_OUTPUT_HEIGHT 720
#define GST_CAPS_FEATURES_NVMM "memory:NVMM"
#define MAX_SOURCES 128
#define FLUSH_INTERVAL_US 200000

#define RETURN_ON_PARSER_ERROR(parse_expr) \
    if (NVDS_YAML_PARSER_SUCCESS != parse_expr) { \
        g_printerr("Error in parsing configuration file.\n"); \
        return -1; \
    }

gchar pgie_classes_str[4][32] = {"Vehicle", "TwoWheeler", "Person", "RoadSign"};
static int g_face_class_id = 2;
static gboolean g_lpr_mode = FALSE;
static gboolean PERF_MODE = FALSE;
static GMutex redis_mutex;
static redisContext* pub_ctx = NULL;
static int source_to_device[MAX_SOURCES];
static guint64 frame_counts[MAX_SOURCES];
static std::vector<std::string> g_labels;
static const char* g_sources_key = "deepstream:sources:main";

static NvDsObjEncCtxHandle g_face_enc_ctx = NULL;
static int                 g_face_sock_fd  = -1;
static bool                g_face_sock_ok  = false;
static guint64             g_face_obj_ctr  = 0;

struct FacePending {
    NvDsObjectMeta* om;
    NvDsFrameMeta*  fm;
    int             dev_id;
    float           quality;
    float           kps[FACE_KPS_COUNT * 2];
    bool            has_lm;
    bool            has_emb;
};

struct ProbeData {
    SnapshotSender* roi_snap;
    SnapshotSender* lc_snap;
    SnapshotSender* oc_snap;
    guint64 last_snap_time;
};
#define SNAP_COOLDOWN_US 3000000

extern "C" {
    struct Det10gLandmarks {
        float pts[FACE_KPS_COUNT][2];
        float cx, cy;
    };
    int Det10g_GetLandmarks(Det10gLandmarks*, int);
    int Det10g_FindLandmarks(float, float, float, float, Det10gLandmarks*);
}

static std::string parse_analytics_frame_meta(NvDsFrameMeta* fm)
{
    std::stringstream out;
    bool first = true;

    for (NvDsMetaList* l_user = fm->frame_user_meta_list; l_user; l_user = l_user->next) {
        NvDsUserMeta* um = (NvDsUserMeta*)l_user->data;
        if (um->base_meta.meta_type != NVDS_USER_FRAME_META_NVDSANALYTICS)
            continue;

        NvDsAnalyticsFrameMeta* meta = (NvDsAnalyticsFrameMeta*)um->user_meta_data;
        if (!meta) continue;

        for (std::pair<std::string, uint32_t> status : meta->objInROIcnt) {
            if (!first) out << ",";
            out << "\"" << status.first << "_in_ROI\": " << status.second;
            first = false;
        }
        for (std::pair<std::string, uint32_t> status : meta->objLCCurrCnt) {
            if (status.second == 0) continue;
            if (!first) out << ",";
            out << "\"" << status.first << "_LC\": " << status.second;
            first = false;
        }
        for (std::pair<std::string, bool> status : meta->ocStatus) {
            if (!first) out << ",";
            out << "\"" << status.first << "_OC\": " << (status.second ? "true" : "false");
            first = false;
        }
    }
    return out.str();
}

static void redis_hset(const char* k, const char* f, const char* v)
{
    g_mutex_lock(&redis_mutex);
    if (pub_ctx) {
        redisReply* r = (redisReply*)redisCommand(pub_ctx, "HSET %s %s %s", k, f, v);
        if (r) freeReplyObject(r);
    }
    g_mutex_unlock(&redis_mutex);
}

static float compute_quality_score(const float* lm)
{
    for (int r = 0; r < 4; r++) {
        int inside = 0;
        for (int i = KEY_REGIONS[r].start; i <= KEY_REGIONS[r].end; i++) {
            float x = lm[i * 2];
            float y = lm[i * 2 + 1];
            if (x >= 0.0f && x <= 1.0f && y >= 0.0f && y <= 1.0f)
                inside++;
        }
        if (inside < KEY_REGIONS[r].min_pts) return 0.0f;
    }
    return 1.0f;
}

static bool extract_tensor_data(NvDsObjectMeta* obj, guint unique_id,
                                 float* out, size_t max_elems)
{
    for (NvDsMetaList* lum = obj->obj_user_meta_list; lum; lum = lum->next) {
        NvDsUserMeta* um = (NvDsUserMeta*)lum->data;
        if (!um || um->base_meta.meta_type != NVDSINFER_TENSOR_OUTPUT_META) continue;
        NvDsInferTensorMeta* tm = (NvDsInferTensorMeta*)um->user_meta_data;
        if (!tm || tm->unique_id != unique_id) continue;
        size_t offset = 0;
        for (guint l = 0; l < tm->num_output_layers && offset < max_elems; l++) {
            if (!tm->out_buf_ptrs_host || !tm->out_buf_ptrs_host[l]) continue;
            NvDsInferLayerInfo* layer = &tm->output_layers_info[l];
            guint n = layer->inferDims.numElements;
            if (n == 0 || n > 10000) continue;
            float* buf = (float*)tm->out_buf_ptrs_host[l];
            if (!buf) continue;
            size_t copy = (offset + n <= max_elems) ? n : (max_elems - offset);
            memcpy(out + offset, buf, copy * sizeof(float));
            offset += copy;
        }
        return offset > 0;
    }
    return false;
}

static bool connect_face_receiver()
{
    if (g_face_sock_ok) return true;
    if (g_face_sock_fd >= 0) { close(g_face_sock_fd); g_face_sock_fd = -1; }

    struct addrinfo hints = {}, *res = NULL;
    hints.ai_family   = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char port_str[8];
    g_snprintf(port_str, sizeof(port_str), "%d", FACE_RECEIVER_PORT);
    if (getaddrinfo(FACE_RECEIVER_HOST, port_str, &hints, &res) != 0 || !res) {
        g_printerr("[Face] DNS lookup failed for %s\n", FACE_RECEIVER_HOST);
        return false;
    }
    int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) { freeaddrinfo(res); return false; }
    if (connect(fd, res->ai_addr, res->ai_addrlen) < 0) {
        close(fd);
        freeaddrinfo(res);
        g_printerr("[Face] connect to %s:%d failed\n", FACE_RECEIVER_HOST, FACE_RECEIVER_PORT);
        return false;
    }
    freeaddrinfo(res);
    g_face_sock_fd = fd;
    g_face_sock_ok = true;
    g_print("[Face] Connected to %s:%d\n", FACE_RECEIVER_HOST, FACE_RECEIVER_PORT);
    return true;
}

static void close_face_socket()
{
    if (g_face_sock_fd >= 0) { close(g_face_sock_fd); g_face_sock_fd = -1; }
    g_face_sock_ok = false;
}

static bool send_face_crop(const FacePending& fp)
{
    if (!g_face_sock_ok && !connect_face_receiver()) return false;

    for (NvDsMetaList* lum = fp.om->obj_user_meta_list; lum; lum = lum->next) {
        NvDsUserMeta* um = (NvDsUserMeta*)lum->data;
        if (!um || um->base_meta.meta_type != NVDS_CROP_IMAGE_META) continue;
        NvDsObjEncOutParams* enc = (NvDsObjEncOutParams*)um->user_meta_data;
        if (!enc || !enc->outBuffer || enc->outLen == 0) continue;

        float fw = (float)MUXER_OUTPUT_WIDTH;
        float fh = (float)MUXER_OUTPUT_HEIGHT;

        FaceCropPacket pkt;
        pkt.device_id    = (uint32_t)fp.dev_id;
        pkt.object_id    = fp.om->object_id;
        pkt.quality_score = fp.quality;
        pkt.bbox_left    = fp.om->detector_bbox_info.org_bbox_coords.left   / fw;
        pkt.bbox_top     = fp.om->detector_bbox_info.org_bbox_coords.top    / fh;
        pkt.bbox_width   = fp.om->detector_bbox_info.org_bbox_coords.width  / fw;
        pkt.bbox_height  = fp.om->detector_bbox_info.org_bbox_coords.height / fh;
        pkt.timestamp_ms = (uint64_t)(time(nullptr) * 1000LL);
        memcpy(pkt.kps, fp.kps, sizeof(pkt.kps));

        auto safe_send = [&](const void* data, size_t len) -> bool {
            ssize_t s = send(g_face_sock_fd, data, len, MSG_NOSIGNAL);
            if (s < 0) { close_face_socket(); return false; }
            return true;
        };

        if (!safe_send(enc->outBuffer, enc->outLen)) return false;
        if (!safe_send(FACE_END_MARKER, strlen(FACE_END_MARKER))) return false;
        if (!safe_send(&pkt, sizeof(pkt))) return false;

        return true;
    }
    return false;
}

static GstPadProbeReturn tiler_src_pad_buffer_probe(GstPad* pad, GstPadProbeInfo* info,
                                                      gpointer u_data)
{
    return GST_PAD_PROBE_OK;
}

static GstPadProbeReturn analytics_pad_probe(GstPad* pad, GstPadProbeInfo* info,
                                              gpointer user_data)
{
    static guint64 last_flush = 0, last_health = 0, last_reload = 0;

    ProbeData* pd = (ProbeData*)user_data;
    GstBuffer* buf = GST_BUFFER(info->data);
    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) return GST_PAD_PROBE_OK;

    guint64 now = g_get_monotonic_time();

    if (now - last_reload >= 5000000 && pub_ctx) {
        redisReply* r = (redisReply*)redisCommand(pub_ctx, "HGETALL %s", g_sources_key);
        if (r && r->type == REDIS_REPLY_ARRAY) {
            g_mutex_lock(&redis_mutex);
            memset(source_to_device, -1, sizeof(source_to_device));
            for (size_t k = 0; k + 1 < r->elements; k += 2) {
                const char* key = r->element[k]->str;
                if (strchr(key, ':')) continue;
                int sid = atoi(key);
                int dev = atoi(r->element[k + 1]->str);
                if (sid >= 0 && sid < MAX_SOURCES) source_to_device[sid] = dev;
            }
            g_mutex_unlock(&redis_mutex);
        }
        if (r) freeReplyObject(r);
        last_reload = now;
    }

    for (NvDsMetaList* lf = batch_meta->frame_meta_list; lf; lf = lf->next) {
        NvDsFrameMeta* fm = (NvDsFrameMeta*)lf->data;
        int sid = fm->source_id;
        if (sid >= 0 && sid < MAX_SOURCES) frame_counts[sid]++;

        int dev_id = (sid >= 0 && sid < MAX_SOURCES) ? source_to_device[sid] : -1;
        if (dev_id < 0) continue;

        if (now - last_flush >= FLUSH_INTERVAL_US) {
            gchar json_buf[65536];
            int off = g_snprintf(json_buf, sizeof(json_buf),
                "{\"code\":\"DeepStreamDetection\",\"action\":\"Pulse\","
                "\"timestamp\":%lld,\"data\":{\"device_id\":%d,\"source\":%d,"
                "\"frame_num\":0,\"Object\":[",
                (long long)(time(NULL) * 1000LL), dev_id, sid);

            float fw = (float)MUXER_OUTPUT_WIDTH;
            float fh = (float)MUXER_OUTPUT_HEIGHT;

            int obj_count = 0;

            bool has_obj_in_any_roi = false;
            bool has_obj_in_any_lc = false;
            bool has_obj_in_any_oc = false;
            for (NvDsMetaList* lfu = fm->frame_user_meta_list; lfu; lfu = lfu->next) {
                NvDsUserMeta* fum = (NvDsUserMeta*)lfu->data;
                if (fum->base_meta.meta_type == NVDS_USER_FRAME_META_NVDSANALYTICS) {
                    NvDsAnalyticsFrameMeta* afm =
                        (NvDsAnalyticsFrameMeta*)fum->user_meta_data;
                    if (afm && !afm->objInROIcnt.empty()) {
                        has_obj_in_any_roi = true;
                    }
                    if (afm && !afm->objLCCurrCnt.empty()) {
                        for (auto& p : afm->objLCCurrCnt) {
                            if (p.second > 0) { has_obj_in_any_lc = true; break; }
                        }
                    }
                    if (afm && !afm->ocStatus.empty()) {
                        for (auto& p : afm->ocStatus) {
                            if (p.second) { has_obj_in_any_oc = true; break; }
                        }
                    }
                }
            }

            for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
                NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
                float left = om->detector_bbox_info.org_bbox_coords.left;
                float top = om->detector_bbox_info.org_bbox_coords.top;
                float width = om->detector_bbox_info.org_bbox_coords.width;
                float height = om->detector_bbox_info.org_bbox_coords.height;

                int rx1 = (int)(left / fw * 1600);
                int ry1 = (int)(top / fh * 900);
                int rx2 = (int)((left + width) / fw * 1600);
                int ry2 = (int)((top + height) / fh * 900);

                const char* label = om->class_id < (int)g_labels.size()
                    ? g_labels[om->class_id].c_str() : "unknown";
                const char* id_field = (strstr(label, "person") || strstr(label, "face"))
                    ? "HumamID" : "VehicleID";

                off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                    "%s{\"object_id\":%lu,\"class_id\":%d,"
                    "\"class_label\":\"%s\",\"confidence\":%.4f,"
                    "\"bbox\":{\"left\":%.4f,\"top\":%.4f,"
                    "\"width\":%.4f,\"height\":%.4f},"
                    "\"%s\":%lu,"
                    "\"Rect\":[%d,%d,%d,%d]",
                    obj_count > 0 ? "," : "",
                    om->object_id, om->class_id,
                    label, om->confidence,
                    left / fw, top / fh, width / fw, height / fh,
                    id_field, om->object_id,
                    rx1, ry1, rx2, ry2);

                NvDsAnalyticsObjInfo* aoi = nullptr;
                for (NvDsMetaList* lum = om->obj_user_meta_list; lum; lum = lum->next) {
                    NvDsUserMeta* um = (NvDsUserMeta*)lum->data;
                    if (um->base_meta.meta_type == NVDS_USER_OBJ_META_NVDSANALYTICS) {
                        aoi = (NvDsAnalyticsObjInfo*)um->user_meta_data;
                        break;
                    }
                }

                if (has_obj_in_any_roi) {
                    om->rect_params.border_color.red = 0.0;
                    om->rect_params.border_color.green = 1.0;
                    om->rect_params.border_color.blue = 0.0;
                    om->rect_params.border_color.alpha = 1.0;
                }

                if (aoi) {
                    if (!aoi->roiStatus.empty()) {
                        om->rect_params.border_color.red = 0.0;
                        om->rect_params.border_color.green = 1.0;
                        om->rect_params.border_color.blue = 0.0;
                        om->rect_params.border_color.alpha = 1.0;

                        off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                            ",\"roi\":[");
                        for (size_t r = 0; r < aoi->roiStatus.size(); r++) {
                            off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                                "%s\"%s\"", r > 0 ? "," : "", aoi->roiStatus[r].c_str());
                        }
                        off += g_snprintf(json_buf + off, sizeof(json_buf) - off, "]");
                    }
                    if (!aoi->ocStatus.empty()) {
                        om->rect_params.border_color.red = 1.0;
                        om->rect_params.border_color.green = 0.0;
                        om->rect_params.border_color.blue = 1.0;
                        om->rect_params.border_color.alpha = 1.0;

                        off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                            ",\"oc\":[");
                        for (size_t o = 0; o < aoi->ocStatus.size(); o++) {
                            off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                                "%s\"%s\"", o > 0 ? "," : "", aoi->ocStatus[o].c_str());
                        }
                        off += g_snprintf(json_buf + off, sizeof(json_buf) - off, "]");
                    }
                    if (!aoi->lcStatus.empty()) {
                        om->rect_params.border_color.red = 0.0;
                        om->rect_params.border_color.green = 1.0;
                        om->rect_params.border_color.blue = 1.0;
                        om->rect_params.border_color.alpha = 1.0;

                        off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                            ",\"lc\":[");
                        for (size_t l = 0; l < aoi->lcStatus.size(); l++) {
                            off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                                "%s\"%s\"", l > 0 ? "," : "", aoi->lcStatus[l].c_str());
                        }
                        off += g_snprintf(json_buf + off, sizeof(json_buf) - off, "]");
                    }
                    if (!aoi->dirStatus.empty()) {
                        om->rect_params.border_color.red = 1.0;
                        om->rect_params.border_color.green = 1.0;
                        om->rect_params.border_color.blue = 0.0;
                        om->rect_params.border_color.alpha = 1.0;

                        off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                            ",\"direction\":\"%s\"", aoi->dirStatus.c_str());
                    }
                }

                if (g_lpr_mode) {
                    for (NvDsMetaList* lc = om->classifier_meta_list; lc; lc = lc->next) {
                        NvDsClassifierMeta* cm = (NvDsClassifierMeta*)lc->data;
                        if (cm->unique_component_id != 3) continue;
                        for (NvDsMetaList* ll = cm->label_info_list; ll; ll = ll->next) {
                            NvDsLabelInfo* li = (NvDsLabelInfo*)ll->data;
                            if (li->result_label && li->result_label[0]) {
                                off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                                    ",\"plate\":\"%s\"", li->result_label);
                            }
                        }
                    }
                }

                off += g_snprintf(json_buf + off, sizeof(json_buf) - off, "}");

                obj_count++;
            }
            off += g_snprintf(json_buf + off, sizeof(json_buf) - off, "]");

            std::string afm = parse_analytics_frame_meta(fm);
            if (!afm.empty()) {
                off += g_snprintf(json_buf + off, sizeof(json_buf) - off,
                    ",\"analytics\":{%s}", afm.c_str());
            }

            off += g_snprintf(json_buf + off, sizeof(json_buf) - off, "}}");

            if (obj_count > 0) {
                g_mutex_lock(&redis_mutex);
                if (pub_ctx) {
                    gchar ch[64];
                    g_snprintf(ch, sizeof(ch), "device:%d:events", dev_id);
                    redisReply* r = (redisReply*)redisCommand(pub_ctx,
                        "PUBLISH %s %s", ch, json_buf);
                    if (r) freeReplyObject(r);
                }
                g_mutex_unlock(&redis_mutex);
                g_print("[Analytics] device=%d objects=%d\n", dev_id, obj_count);
            }

            if (pd && fm->obj_meta_list && obj_count > 0 &&
                (has_obj_in_any_roi || has_obj_in_any_lc || has_obj_in_any_oc)) {

                if (now - pd->last_snap_time >= SNAP_COOLDOWN_US) {
                    GstMapInfo inmap = GST_MAP_INFO_INIT;
                    if (gst_buffer_map(buf, &inmap, GST_MAP_READ)) {
                        NvBufSurface* surf = (NvBufSurface*)inmap.data;
                        if (surf) {
                            if (has_obj_in_any_roi && pd->roi_snap) {
                                pd->roi_snap->send_full_frame(
                                    surf, fm, dev_id, sid);
                            }
                            if (has_obj_in_any_lc && pd->lc_snap) {
                                pd->lc_snap->send_full_frame(
                                    surf, fm, dev_id, sid);
                            }
                            if (has_obj_in_any_oc && pd->oc_snap) {
                                pd->oc_snap->send_full_frame(
                                    surf, fm, dev_id, sid);
                            }
                        }
                        gst_buffer_unmap(buf, &inmap);
                    }
                    pd->last_snap_time = now;
                }
            }

            if (g_face_enc_ctx) {
                static guint64 last_face_sent = 0;
                guint64 face_interval = 1000000 / FACE_MAX_FPS;
                std::vector<FacePending> face_queue;

                for (NvDsMetaList* lo = fm->obj_meta_list; lo; lo = lo->next) {
                    NvDsObjectMeta* om = (NvDsObjectMeta*)lo->data;
                    if (om->class_id != g_face_class_id) continue;

                    float w = om->detector_bbox_info.org_bbox_coords.width;
                    float h = om->detector_bbox_info.org_bbox_coords.height;
                    if (w < FACE_MIN_BBOX_PX || h < FACE_MIN_BBOX_PX) continue;

                    if (now - last_face_sent < face_interval) continue;

                    FacePending fp;
                    fp.om      = om;
                    fp.fm      = fm;
                    fp.dev_id  = dev_id;
                    fp.quality = 1.0f;
                    fp.has_lm  = false;
                    fp.has_emb = false;
                    memset(fp.kps, 0, sizeof(fp.kps));

                    float left = om->detector_bbox_info.org_bbox_coords.left;
                    float top  = om->detector_bbox_info.org_bbox_coords.top;

                    Det10gLandmarks lm;
                    if (Det10g_FindLandmarks(left, top, w, h, &lm)) {
                        for (int k = 0; k < FACE_KPS_COUNT; k++) {
                            fp.kps[k * 2]     = lm.pts[k][0];
                            fp.kps[k * 2 + 1] = lm.pts[k][1];
                        }
                        fp.has_lm = true;
                    }

                    NvDsObjEncUsrArgs objData = {};
                    objData.saveImg      = FALSE;
                    objData.attachUsrMeta = TRUE;
                    objData.quality      = 80;
                    objData.objNum       = (int)(++g_face_obj_ctr);

                    GstMapInfo inmap = GST_MAP_INFO_INIT;
                    if (!gst_buffer_map(buf, &inmap, GST_MAP_READ)) continue;
                    NvBufSurface* surf = (NvBufSurface*)inmap.data;
                    if (surf) {
                        nvds_obj_enc_process(g_face_enc_ctx, &objData,
                                             surf, om, fm);
                        face_queue.push_back(fp);
                    }
                    gst_buffer_unmap(buf, &inmap);
                    last_face_sent = now;
                }

                if (!face_queue.empty()) {
                    nvds_obj_enc_finish(g_face_enc_ctx);
                    for (auto& fp : face_queue) {
                        if (!send_face_crop(fp)) {
                            g_printerr("[Face] send failed for object %lu\n",
                                       fp.om->object_id);
                        } else {
                            g_print("[Face] device=%d object=%lu\n",
                                    fp.dev_id, fp.om->object_id);
                        }
                    }
                }
            }
        }
    }

    if (now - last_flush >= FLUSH_INTERVAL_US) last_flush = now;

    if (now - last_health >= 1000000 && pub_ctx) {
        g_mutex_lock(&redis_mutex);
        gdouble elapsed = (gdouble)(now - last_health) / 1000000.0;
        for (int i = 0; i < MAX_SOURCES; i++) {
            if (frame_counts[i] > 0) {
                gchar k[32];
                g_snprintf(k, sizeof(k), "%d:fps", i);
                redisReply* r = (redisReply*)redisCommand(pub_ctx,
                    "HSET %s %s %d", g_sources_key, k,
                    (int)(frame_counts[i] / elapsed + 0.5));
                if (r) freeReplyObject(r);
                frame_counts[i] = 0;
            }
        }
        last_health = now;
        g_mutex_unlock(&redis_mutex);
    }

    return GST_PAD_PROBE_OK;
}

static gboolean bus_call(GstBus* bus, GstMessage* msg, gpointer data)
{
    GMainLoop* loop = (GMainLoop*)data;
    switch (GST_MESSAGE_TYPE(msg)) {
        case GST_MESSAGE_EOS:
            g_print("End of stream\n");
            g_main_loop_quit(loop);
            break;
        case GST_MESSAGE_WARNING: {
            gchar* debug = NULL;
            GError* error = NULL;
            gst_message_parse_warning(msg, &error, &debug);
            g_printerr("WARNING from %s: %s\n", GST_OBJECT_NAME(msg->src), error->message);
            g_free(debug);
            g_error_free(error);
            break;
        }
        case GST_MESSAGE_ERROR: {
            gchar* debug = NULL;
            GError* error = NULL;
            gst_message_parse_error(msg, &error, &debug);
            const gchar* src_name = GST_OBJECT_NAME(msg->src);
            g_printerr("ERROR from %s: %s\n", src_name, error->message);
            if (debug) g_printerr("Details: %s\n", debug);
            g_free(debug);
            g_error_free(error);
            // Allow source/RTSP errors to be non-fatal — rtspsrc auto-reconnects
            if (g_str_has_prefix(src_name, "source-bin-") ||
                g_str_has_prefix(src_name, "source")) {
                g_printerr("(source error — pipeline continues, source will reconnect)\n");
                break;
            }
            g_main_loop_quit(loop);
            break;
        }
        default:
            break;
    }
    return TRUE;
}

static void cb_newpad(GstElement* decodebin, GstPad* decoder_src_pad, gpointer data)
{
    GstCaps* caps = gst_pad_get_current_caps(decoder_src_pad);
    if (!caps) caps = gst_pad_query_caps(decoder_src_pad, NULL);
    const GstStructure* str = gst_caps_get_structure(caps, 0);
    const gchar* name = gst_structure_get_name(str);
    GstElement* source_bin = (GstElement*)data;
    GstCapsFeatures* features = gst_caps_get_features(caps, 0);

    if (!strncmp(name, "video", 5)) {
        if (gst_caps_features_contains(features, GST_CAPS_FEATURES_NVMM)) {
            GstPad* ghost = gst_element_get_static_pad(source_bin, "src");
            gst_ghost_pad_set_target(GST_GHOST_PAD(ghost), decoder_src_pad);
            gst_object_unref(ghost);
        } else {
            g_printerr("Decodebin did not pick nvidia decoder.\n");
        }
    }
}

static void decodebin_child_added(GstChildProxy* child_proxy, GObject* object,
                                   gchar* name, gpointer user_data)
{
    g_print("Decodebin child added: %s\n", name);
    if (!strncmp(name, "decodebin", 9)) {
        g_signal_connect(G_OBJECT(object), "child-added",
                         G_CALLBACK(decodebin_child_added), user_data);
    }
    // Configure rtspsrc for TCP transport + auto-reconnect
    if (g_str_has_prefix(name, "source")) {
        g_object_set(G_OBJECT(object),
            "protocols", 4,            // 4 = TCP only (avoids UDP buffer issues)
            "retry", 100,              // unlimited retries on disconnect
            "timeout", 5000000,        // 5 seconds
            "tcp-timeout", 5000000,    // 5 seconds
            "latency", 200,            // 200ms jitter buffer
            "drop-on-latency", FALSE,  // never drop frames
            NULL);
        g_print("  -> rtspsrc configured: TCP mode, auto-retry\n");
    }
}

static GstElement* create_source_bin(guint index, gchar* uri)
{
    gchar bin_name[16];
    g_snprintf(bin_name, 15, "source-bin-%02d", index);
    GstElement* bin = gst_bin_new(bin_name);
    GstElement* uri_decode_bin = gst_element_factory_make("uridecodebin", "uri-decode-bin");

    if (!bin || !uri_decode_bin) return NULL;

    g_object_set(G_OBJECT(uri_decode_bin), "uri", uri, "use-buffering", FALSE, NULL);
    g_signal_connect(G_OBJECT(uri_decode_bin), "pad-added", G_CALLBACK(cb_newpad), bin);
    g_signal_connect(G_OBJECT(uri_decode_bin), "child-added",
                     G_CALLBACK(decodebin_child_added), bin);
    gst_bin_add(GST_BIN(bin), uri_decode_bin);
    gst_element_add_pad(bin, gst_ghost_pad_new_no_target("src", GST_PAD_SRC));

    return bin;
}

int main(int argc, char* argv[])
{
    GMainLoop* loop = NULL;
    GstElement *pipeline = NULL, *streammux = NULL, *sink = NULL, *pgie = NULL,
               *nvtracker = NULL,
               *queue1, *queue2, *queue3, *queue4, *queue5,
               *nvvidconv = NULL, *nvosd = NULL, *tiler = NULL,
               *nvds_analytics = NULL;
    GstElement *sgie0 = NULL, *sgie1 = NULL;
    GstElement *q_sgie0 = NULL, *q_sgie1 = NULL;
    GstBus* bus = NULL;
    guint bus_watch_id;
    guint i, num_sources = 0;
    guint tiler_rows, tiler_columns;
    guint pgie_batch_size;
    gboolean yaml_config = FALSE;
    NvDsGieType pgie_type = NVDS_GIE_PLUGIN_INFER;

    int current_device = -1;
    cudaGetDevice(&current_device);
    struct cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, current_device);

    const char* enable_display_env = getenv("ENABLE_DISPLAY");
    int show_display = enable_display_env ? atoi(enable_display_env) : 1;

    const char* sources_key_env = getenv("DEEPSTREAM_SOURCES_KEY");
    if (sources_key_env) g_sources_key = sources_key_env;

    if (argc < 2) {
        g_printerr("Usage: %s <yml file>\n", argv[0]);
        return -1;
    }

    gst_init(&argc, &argv);
    loop = g_main_loop_new(NULL, FALSE);

    yaml_config = (g_str_has_suffix(argv[1], ".yml") || g_str_has_suffix(argv[1], ".yaml"));
    if (yaml_config) {
        RETURN_ON_PARSER_ERROR(nvds_parse_gie_type(&pgie_type, argv[1], "primary-gie"));
    }

    g_face_class_id = yaml_read_int(argv[1], "face-class-id", 2);
    g_lpr_mode = yaml_read_int(argv[1], "lpr-pipeline", 0) ? TRUE : FALSE;
    yaml_read_labels(argv[1], g_labels);
    g_print("[Pipeline] Face class ID: %d LPR mode: %s labels: %d\n",
            g_face_class_id, g_lpr_mode ? "yes" : "no", (int)g_labels.size());

    pipeline = gst_pipeline_new("analytics-pipeline");
    streammux = gst_element_factory_make("nvstreammux", "stream-muxer");
    if (!pipeline || !streammux) return -1;
    gst_bin_add(GST_BIN(pipeline), streammux);

    GList* src_list = NULL;
    if (yaml_config) {
        RETURN_ON_PARSER_ERROR(nvds_parse_source_list(&src_list, argv[1], "source-list"));
        GList* temp = src_list;
        while (temp) { num_sources++; temp = temp->next; }
        g_list_free(temp);
    } else {
        num_sources = argc - 1;
    }

    for (i = 0; i < num_sources; i++) {
        GstPad *sinkpad, *srcpad;
        gchar pad_name[16] = {};
        GstElement* source_bin = NULL;

        if (yaml_config) {
            if (!src_list || !src_list->data) continue;
            source_bin = create_source_bin(i, (gchar*)src_list->data);
        } else {
            source_bin = create_source_bin(i, argv[i + 1]);
        }
        if (!source_bin) return -1;

        gst_bin_add(GST_BIN(pipeline), source_bin);
        g_snprintf(pad_name, 15, "sink_%u", i);
        sinkpad = gst_element_request_pad_simple(streammux, pad_name);
        if (!sinkpad) return -1;
        srcpad = gst_element_get_static_pad(source_bin, "src");
        if (!srcpad || gst_pad_link(srcpad, sinkpad) != GST_PAD_LINK_OK) {
            if (srcpad) gst_object_unref(srcpad);
            gst_object_unref(sinkpad);
            return -1;
        }
        gst_object_unref(srcpad);
        gst_object_unref(sinkpad);
        if (yaml_config && src_list) src_list = src_list->next;
    }
    if (yaml_config) g_list_free(src_list);

    if (pgie_type == NVDS_GIE_PLUGIN_INFER_SERVER)
        pgie = gst_element_factory_make("nvinferserver", "primary-nvinference-engine");
    else
        pgie = gst_element_factory_make("nvinfer", "primary-nvinference-engine");

    queue1 = gst_element_factory_make("queue", "queue1");
    queue2 = gst_element_factory_make("queue", "queue2");
    nvtracker = gst_element_factory_make("nvtracker", "nvtracker");
    nvds_analytics = gst_element_factory_make("nvdsanalytics", "nvdsanalytics");

    if (show_display) {
        queue3 = gst_element_factory_make("queue", "queue3");
        queue4 = gst_element_factory_make("queue", "queue4");
        queue5 = gst_element_factory_make("queue", "queue5");
        tiler = gst_element_factory_make("nvmultistreamtiler", "nvtiler");
        nvvidconv = gst_element_factory_make("nvvideoconvert", "nvvideo-converter");
        nvosd = gst_element_factory_make("nvdsosd", "nv-onscreendisplay");
        sink = gst_element_factory_make("nveglglessink", "nvvideo-renderer");
    } else {
        queue3 = NULL;
        queue4 = NULL;
        queue5 = NULL;
        sink = gst_element_factory_make("fakesink", "fake-sink");
    }

    if (!pgie || !nvtracker || !nvds_analytics || !sink) return -1;
    if (show_display && (!tiler || !nvvidconv || !nvosd)) return -1;

    if (yaml_config) {
        if (yaml_has_section(argv[1], "secondary-gie0")) {
            sgie0 = gst_element_factory_make("nvinfer", "secondary-gie0");
            q_sgie0 = gst_element_factory_make("queue", "queue-sgie0");
            if (!sgie0 || !q_sgie0) {
                g_printerr("SGIE0 creation failed.\n");
                return -1;
            }
            RETURN_ON_PARSER_ERROR(nvds_parse_gie(sgie0, argv[1], "secondary-gie0"));
            g_print("[Pipeline] SGIE0 (secondary-gie0) configured\n");
        }
        if (yaml_has_section(argv[1], "secondary-gie1")) {
            sgie1 = gst_element_factory_make("nvinfer", "secondary-gie1");
            q_sgie1 = gst_element_factory_make("queue", "queue-sgie1");
            if (!sgie1 || !q_sgie1) {
                g_printerr("SGIE1 creation failed.\n");
                return -1;
            }
            RETURN_ON_PARSER_ERROR(nvds_parse_gie(sgie1, argv[1], "secondary-gie1"));
            g_print("[Pipeline] SGIE1 (secondary-gie1) configured\n");
        }
    }

    if (yaml_config) {
        RETURN_ON_PARSER_ERROR(nvds_parse_streammux(streammux, argv[1], "streammux"));
        RETURN_ON_PARSER_ERROR(nvds_parse_gie(pgie, argv[1], "primary-gie"));
        g_object_get(G_OBJECT(pgie), "batch-size", &pgie_batch_size, NULL);
        if (num_sources > 0) {
            guint target = MIN(pgie_batch_size, num_sources);
            g_object_set(G_OBJECT(pgie), "batch-size", target, NULL);
        }
        g_object_set(G_OBJECT(nvtracker),
                     "tracker-width", 640,
                     "tracker-height", 384,
                     "ll-lib-file",
                     "/opt/nvidia/deepstream/deepstream-8.0/lib/libnvds_nvmultiobjecttracker.so",
                     "ll-config-file", "config_tracker_IOU.yml",
                     NULL);
        g_object_set(G_OBJECT(nvds_analytics),
                     "enable", TRUE,
                     "config-file", "config_nvdsanalytics.txt",
                     NULL);
        if (show_display) {
            RETURN_ON_PARSER_ERROR(nvds_parse_osd(nvosd, argv[1], "osd"));
            if (num_sources > 0) {
                tiler_rows = (guint)sqrt(num_sources);
                tiler_columns = (guint)ceil(1.0 * num_sources / tiler_rows);
                g_object_set(G_OBJECT(tiler), "rows", tiler_rows, "columns", tiler_columns, NULL);
            }
            RETURN_ON_PARSER_ERROR(nvds_parse_tiler(tiler, argv[1], "tiler"));
            RETURN_ON_PARSER_ERROR(nvds_parse_egl_sink(sink, argv[1], "sink"));
        }
    }

    bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline));
    bus_watch_id = gst_bus_add_watch(bus, bus_call, loop);
    gst_object_unref(bus);

    if (show_display) {
        gst_bin_add_many(GST_BIN(pipeline), queue1, pgie, queue2, nvtracker,
                         nvds_analytics,
                         tiler,
                         queue3, nvvidconv, queue4, nvosd, queue5, sink, NULL);
        if (sgie0) gst_bin_add_many(GST_BIN(pipeline), q_sgie0, sgie0, NULL);
        if (sgie1) gst_bin_add_many(GST_BIN(pipeline), q_sgie1, sgie1, NULL);

        if (!gst_element_link_many(streammux, queue1, pgie, queue2, nvtracker, NULL)) {
            g_printerr("streammux→nvtracker link failed.\n");
            return -1;
        }
        GstElement* prev = nvtracker;
        if (sgie0) { gst_element_link_many(prev, q_sgie0, sgie0, NULL); prev = sgie0; }
        if (sgie1) { gst_element_link_many(prev, q_sgie1, sgie1, NULL); prev = sgie1; }
        if (!gst_element_link_many(prev,
                                   nvds_analytics,
                                   tiler,
                                   queue3, nvvidconv, queue4, nvosd, queue5, sink, NULL)) {
            g_printerr("Elements could not be linked.\n");
            return -1;
        }
    } else {
        gst_bin_add_many(GST_BIN(pipeline), queue1, pgie, queue2, nvtracker,
                         nvds_analytics,
                         sink, NULL);
        if (sgie0) gst_bin_add_many(GST_BIN(pipeline), q_sgie0, sgie0, NULL);
        if (sgie1) gst_bin_add_many(GST_BIN(pipeline), q_sgie1, sgie1, NULL);
        g_object_set(G_OBJECT(sink), "sync", FALSE, NULL);

        if (!gst_element_link_many(streammux, queue1, pgie, queue2, nvtracker, NULL)) {
            g_printerr("streammux→nvtracker link failed.\n");
            return -1;
        }
        GstElement* prev = nvtracker;
        if (sgie0) { gst_element_link_many(prev, q_sgie0, sgie0, NULL); prev = sgie0; }
        if (sgie1) { gst_element_link_many(prev, q_sgie1, sgie1, NULL); prev = sgie1; }
        if (!gst_element_link_many(prev,
                                   nvds_analytics,
                                   sink, NULL)) {
            g_printerr("Elements could not be linked.\n");
            return -1;
        }
    }

    GstPad* probe_pad = gst_element_get_static_pad(nvds_analytics, "src");

    ProbeData probe_data;
    memset(&probe_data, 0, sizeof(probe_data));
    SnapshotSender roi_snap("snapshot-receiver", 12349, "roi");
    SnapshotSender lc_snap("snapshot-receiver", 12349, "lc");
    SnapshotSender oc_snap("snapshot-receiver", 12349, "oc");

    bool roi_ok = roi_snap.start();
    bool lc_ok = lc_snap.start();
    bool oc_ok = oc_snap.start();
    if (roi_ok) probe_data.roi_snap = &roi_snap;
    if (lc_ok) probe_data.lc_snap = &lc_snap;
    if (oc_ok) probe_data.oc_snap = &oc_snap;
    probe_data.last_snap_time = 0;

    if (sgie0 || sgie1 || g_face_class_id >= 0) {
        if (g_lpr_mode) {
            g_print("[LPR] SGIE pipeline detected, skipping face encoder\n");
        } else {
            g_face_enc_ctx = nvds_obj_enc_create_context(0);
            if (g_face_enc_ctx)
                g_print("[Face] Encoder context created\n");
            else
                g_printerr("[Face] Failed to create encoder context\n");
            connect_face_receiver();
        }
    }

    if (probe_pad) {
        gst_pad_add_probe(probe_pad, GST_PAD_PROBE_TYPE_BUFFER,
                          analytics_pad_probe, &probe_data, NULL);
        g_print("[Pipeline] Analytics probe added on nvdsanalytics src\n");
        gst_object_unref(probe_pad);
    }



    g_mutex_init(&redis_mutex);
    memset(source_to_device, -1, sizeof(source_to_device));
    memset(frame_counts, 0, sizeof(frame_counts));

    pub_ctx = redisConnect("redis", 6379);
    if (!pub_ctx || pub_ctx->err) {
        g_printerr("[Pipeline] Redis connection failed\n");
        if (pub_ctx) { redisFree(pub_ctx); pub_ctx = NULL; }
    } else {
        g_print("[Pipeline] Redis connected\n");
    }

    g_print("Using file: %s\n", argv[1]);
    gst_element_set_state(pipeline, GST_STATE_PLAYING);
    g_print("Running...\n");
    g_main_loop_run(loop);

    g_print("Stopping playback\n");
    gst_element_set_state(pipeline, GST_STATE_NULL);

    roi_snap.stop();
    lc_snap.stop();
    oc_snap.stop();

    close_face_socket();
    if (g_face_enc_ctx) {
        nvds_obj_enc_destroy_context(g_face_enc_ctx);
        g_face_enc_ctx = NULL;
    }

    if (pub_ctx) redisFree(pub_ctx);
    gst_object_unref(GST_OBJECT(pipeline));
    g_source_remove(bus_watch_id);
    g_main_loop_unref(loop);
    return 0;
}
