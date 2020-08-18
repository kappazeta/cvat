
# Copyright (C) 2020 Intel Corporation
#
# SPDX-License-Identifier: MIT

from collections import OrderedDict
from copy import deepcopy
import logging as log

import attr
import cv2
import numpy as np
from attr import attrib, attrs

from datumaro.components.cli_plugin import CliPlugin
from datumaro.components.extractor import AnnotationType, Bbox, Label
from datumaro.components.project import Dataset
from datumaro.util import find
from datumaro.util.attrs_util import ensure_cls
from datumaro.util.annotation_util import (segment_iou, bbox_iou,
    mean_bbox, OKS, find_instances, max_bbox, smooth_line)

def get_ann_type(anns, t):
    return [a for a in anns if a.type == t]

def match_annotations_equal(a, b):
    matches = []
    a_unmatched = a[:]
    b_unmatched = b[:]
    for a_ann in a:
        for b_ann in b_unmatched:
            if a_ann != b_ann:
                continue

            matches.append((a_ann, b_ann))
            a_unmatched.remove(a_ann)
            b_unmatched.remove(b_ann)
            break

    return matches, a_unmatched, b_unmatched

def merge_annotations_equal(a, b):
    matches, a_unmatched, b_unmatched = match_annotations_equal(a, b)
    return [ann_a for (ann_a, _) in matches] + a_unmatched + b_unmatched

def merge_categories(sources):
    categories = {}
    for source in sources:
        categories.update(source)
    for source in sources:
        for cat_type, source_cat in source.items():
            if not categories[cat_type] == source_cat:
                raise NotImplementedError(
                    "Merging different categories is not implemented yet")
    return categories

class MergingStrategy(CliPlugin):
    @classmethod
    def merge(cls, sources, **options):
        instance = cls(**options)
        return instance(sources)

    def __init__(self, **options):
        super().__init__(**options)
        self.__dict__['_sources'] = None

    def __call__(self, sources):
        raise NotImplementedError()


@attrs
class DatasetError:
    item_id = attrib()

@attrs
class QualityError(DatasetError):
    pass

@attrs
class TooCloseError(QualityError):
    a = attrib()
    b = attrib()
    distance = attrib()

    def __str__(self):
        return "Item %s: annotations are too close: %s, %s, distance = %s" % \
            (self.item_id, self.a, self.b, self.distance)

@attrs
class WrongGroupError(QualityError):
    found = attrib(converter=set)
    expected = attrib(converter=set)
    group = attrib(converter=list)

    def __str__(self):
        return "Item %s: annotation group has wrong labels: " \
            "found %s, expected %s, group %s" % \
            (self.item_id, self.found, self.expected, self.group)

@attrs
class MergeError(DatasetError):
    sources = attrib(converter=set)

@attrs
class NoMatchingAnnError(MergeError):
    ann = attrib()

    def __str__(self):
        return "Item %s: can't find matching annotation " \
            "in sources %s, annotation is %s" % \
            (self.item_id, self.sources, self.ann)

@attrs
class NoMatchingItemError(MergeError):
    def __str__(self):
        return "Item %s: can't find matching item in sources %s" % \
            (self.item_id, self.sources)

@attrs
class FailedLabelVotingError(MergeError):
    votes = attrib()
    ann = attrib(default=None)

    def __str__(self):
        return "Item %s: label voting failed%s, votes %s, sources %s" % \
            (self.item_id, 'for ann %s' % self.ann if self.ann else '',
            self.votes, self.sources)

@attrs
class FailedAttrVotingError(MergeError):
    attr = attrib()
    votes = attrib()
    ann = attrib()

    def __str__(self):
        return "Item %s: attribute voting failed " \
            "for ann %s, votes %s, sources %s" % \
            (self.item_id, self.ann, self.votes, self.sources)

@attrs
class IntersectMerge(MergingStrategy):
    @attrs(repr_ns='IntersectMerge', kw_only=True)
    class Conf:
        pairwise_dist = attrib(converter=float, default=0.5)
        sigma = attrib(converter=list, factory=list)

        output_conf_thresh = attrib(converter=float, default=0)
        quorum = attrib(converter=int, default=0)
        ignored_attributes = attrib(converter=set, factory=set)

        def _groups_conveter(value):
            result = []
            for group in value:
                rg = set()
                for label in group:
                    optional = label.endswith('?')
                    name = label if not optional else label[:-1]
                    rg.add((name, optional))
                result.append(rg)
            return result
        groups = attrib(converter=_groups_conveter, factory=list)
        close_distance = attrib(converter=float, default=0.75)
    conf = attrib(converter=ensure_cls(Conf), factory=Conf)

    # Error trackers:
    errors = attrib(factory=list, init=False)
    def add_item_error(self, error, *args, **kwargs):
        self.errors.append(error(self._item_id, *args, **kwargs))

    # Indexes:
    _dataset_map = attrib(init=False) # id(dataset) -> (dataset, index)
    _item_map = attrib(init=False) # id(item) -> (item, id(dataset))
    _ann_map = attrib(init=False) # id(ann) -> (ann, id(item))
    _item_id = attrib(init=False)
    _item = attrib(init=False)

    # Misc.
    _categories = attrib(init=False) # merged categories

    def __call__(self, datasets):
        self._categories = merge_categories(d.categories() for d in datasets)
        merged = Dataset(categories=self._categories)

        self._check_groups_definition()

        item_matches, item_map = self.match_items(datasets)
        self._item_map = item_map
        self._dataset_map = { id(d): (d, i) for i, d in enumerate(datasets) }

        for item_id, items in item_matches.items():
            self._item_id = item_id

            if len(items) < len(datasets):
                missing_sources = set(id(s) for s in datasets) - set(items)
                missing_sources = [self._dataset_map[s][1]
                    for s in missing_sources]
                self.add_item_error(NoMatchingItemError, missing_sources)
            merged.put(self.merge_items(items))

        return merged

    def get_ann_source(self, ann_id):
        return self._item_map[self._ann_map[ann_id][1]][1]

    def merge_items(self, items):
        self._item = next(iter(items.values()))

        self._ann_map = {}
        sources = []
        for item in items.values():
            self._ann_map.update({ id(a): (a, id(item))
                for a in item.annotations })
            sources.append(item.annotations)
        log.debug("Merging item %s: source annotations %s" % \
            (self._item_id, list(map(len, sources))))

        annotations = self.merge_annotations(sources)

        annotations = [a for a in annotations
            if self.conf.output_conf_thresh <= a.attributes.get('score', 1)]

        return self._item.wrap(annotations=annotations)

    def merge_annotations(self, sources):
        self._make_mergers(sources)

        clusters = self._match_annotations(sources)

        joined_clusters = sum(clusters.values(), [])
        group_map = self._find_cluster_groups(joined_clusters)

        annotations = []
        for t, clusters in clusters.items():
            for cluster in clusters:
                self._check_cluster_sources(cluster)

            merged_clusters = self._merge_clusters(t, clusters)

            for merged_ann, cluster in zip(merged_clusters, clusters):
                attributes = self._find_cluster_attrs(cluster, merged_ann)
                attributes = { k: v for k, v in attributes.items()
                    if k not in self.conf.ignored_attributes }
                attributes.update(merged_ann.attributes)
                merged_ann.attributes = attributes

                new_group_id = find(enumerate(group_map),
                    lambda e: id(cluster) in e[1][0])
                if new_group_id is None:
                    new_group_id = 0
                else:
                    new_group_id = new_group_id[0] + 1
                merged_ann.group = new_group_id

            if self.conf.close_distance:
                self._check_annotation_distance(t, merged_clusters)

            annotations += merged_clusters

        if self.conf.groups:
            self._check_groups(annotations)

        return annotations

    @staticmethod
    def match_items(datasets):
        item_ids = set((item.id, item.subset) for d in datasets for item in d)

        item_map = {} # id(item) -> (item, id(dataset))

        matches = OrderedDict()
        for (item_id, item_subset) in sorted(item_ids, key=lambda e: e[0]):
            items = {}
            for d in datasets:
                try:
                    item = d.get(item_id, subset=item_subset)
                    items[id(d)] = item
                    item_map[id(item)] = (item, id(d))
                except KeyError:
                    pass
            matches[(item_id, item_subset)] = items

        return matches, item_map

    def _match_annotations(self, sources):
        all_by_type = {}
        for s in sources:
            src_by_type = {}
            for a in s:
                src_by_type.setdefault(a.type, []).append(a)
            for k, v in src_by_type.items():
                all_by_type.setdefault(k, []).append(v)

        clusters = {}
        for k, v in all_by_type.items():
            clusters.setdefault(k, []).extend(self._match_ann_type(k, v))

        return clusters

    def _make_mergers(self, sources):
        def _make(c, **kwargs):
            kwargs.update(attr.asdict(self.conf))
            fields = attr.fields_dict(c)
            return c(**{ k: v for k, v in kwargs.items() if k in fields },
                context=self)

        def _for_type(t, **kwargs):
            if t is AnnotationType.label:
                return _make(LabelMerger, **kwargs)
            elif t is AnnotationType.bbox:
                return _make(BboxMerger, **kwargs)
            elif t is AnnotationType.mask:
                return _make(MaskMerger, **kwargs)
            elif t is AnnotationType.polygon:
                return _make(PolygonMerger, **kwargs)
            elif t is AnnotationType.polyline:
                return _make(LineMerger, **kwargs)
            elif t is AnnotationType.points:
                return _make(PointsMerger, **kwargs)
            elif t is AnnotationType.caption:
                return _make(CaptionsMerger, **kwargs)
            else:
                raise NotImplementedError("Type %s is not supported" % t)

        instance_map = {}
        for s in sources:
            s_instances = find_instances(s)
            for inst in s_instances:
                inst_bbox = max_bbox([a for a in inst if a.type in
                    {AnnotationType.polygon,
                     AnnotationType.mask, AnnotationType.bbox}
                ])
                for ann in inst:
                    instance_map[id(ann)] = [inst, inst_bbox]

        self._mergers = { t: _for_type(t, instance_map=instance_map)
            for t in AnnotationType }

    def _match_ann_type(self, t, sources):
        return self._mergers[t].match_annotations(sources)

    def _merge_clusters(self, t, clusters):
        return self._mergers[t].merge_clusters(clusters)

    @staticmethod
    def _find_cluster_groups(clusters):
        cluster_groups = []
        visited = set()
        for a_idx, cluster_a in enumerate(clusters):
            if a_idx in visited:
                continue
            visited.add(a_idx)

            cluster_group = { id(cluster_a) }

            # find segment groups in the cluster group
            a_groups = set(ann.group for ann in cluster_a)
            for cluster_b in clusters[a_idx+1 :]:
                b_groups = set(ann.group for ann in cluster_b)
                if a_groups & b_groups:
                    a_groups |= b_groups

            # now we know all the segment groups in this cluster group
            # so we can find adjacent clusters
            for b_idx, cluster_b in enumerate(clusters[a_idx+1 :]):
                b_idx = a_idx + 1 + b_idx
                b_groups = set(ann.group for ann in cluster_b)
                if a_groups & b_groups:
                    cluster_group.add( id(cluster_b) )
                    visited.add(b_idx)

            if a_groups == {0}:
                continue # skip annotations without a group
            cluster_groups.append( (cluster_group, a_groups) )
        return cluster_groups

    def _find_cluster_attrs(self, cluster, ann):
        quorum = self.conf.quorum or 0

        # TODO: when attribute types are implemented, add linear
        # interpolation for contiguous values

        attr_votes = {} # name -> { value: score , ... }
        for s in cluster:
            for name, value in s.attributes.items():
                votes = attr_votes.get(name, {})
                votes[value] = 1 + votes.get(value, 0)
                attr_votes[name] = votes

        attributes = {}
        for name, votes in attr_votes.items():
            winner, count = max(votes.items(), key=lambda e: e[1])
            if count < quorum:
                if sum(votes.values()) < quorum:
                    # blame provokers
                    missing_sources = set(
                        self.get_ann_source(id(a)) for a in cluster
                        if s.attributes.get(name) == winner)
                else:
                    # blame outliers
                    missing_sources = set(
                        self.get_ann_source(id(a)) for a in cluster
                        if s.attributes.get(name) != winner)
                missing_sources = [self._dataset_map[s][1]
                    for s in missing_sources]
                self.add_item_error(FailedAttrVotingError,
                    missing_sources, name, votes, ann)
                continue
            attributes[name] = winner

        return attributes

    def _check_cluster_sources(self, cluster):
        if len(cluster) == len(self._dataset_map):
            return

        def _has_item(s):
            try:
                item =self._dataset_map[s][0].get(*self._item_id)
                if len(item.annotations) == 0:
                    return False
                return True
            except KeyError:
                return False

        missing_sources = set(self._dataset_map) - \
            set(self.get_ann_source(id(a)) for a in cluster)
        missing_sources = [self._dataset_map[s][1] for s in missing_sources
            if _has_item(s)]
        if missing_sources:
            self.add_item_error(NoMatchingAnnError, missing_sources, cluster[0])

    def _check_annotation_distance(self, t, annotations):
        for a_idx, a_ann in enumerate(annotations):
            for b_ann in annotations[a_idx+1:]:
                d = self._mergers[t].distance(a_ann, b_ann)
                if self.conf.close_distance < d:
                    self.add_item_error(TooCloseError, a_ann, b_ann, d)

    def _check_groups(self, annotations):
        check_groups = []
        for check_group_raw in self.conf.groups:
            check_group = set(l[0] for l in check_group_raw)
            optional = set(l[0] for l in check_group_raw if l[1])
            check_groups.append((check_group, optional))

        def _check_group(group_labels, group):
            for check_group, optional in check_groups:
                common = check_group & group_labels
                real_miss = check_group - common - optional
                extra = group_labels - check_group
                if common and (extra or real_miss):
                    self.add_item_error(WrongGroupError, group_labels,
                        check_group, group)
                    break

        groups = find_instances(annotations)
        for group in groups:
            group_labels = set()
            for ann in group:
                if not hasattr(ann, 'label'):
                    continue
                label = self._get_label_name(ann.label)

                if ann.group:
                    group_labels.add(label)
                else:
                    _check_group({label}, [ann])

            if not group_labels:
                continue
            _check_group(group_labels, group)

    def _get_label_name(self, label_id):
        return self._categories[AnnotationType.label].items[label_id].name

    def _check_groups_definition(self):
        for group in self.conf.groups:
            for label, _ in group:
                _, entry = self._categories[AnnotationType.label].find(label)
                if entry is None:
                    raise ValueError("Datasets do not contain "
                        "label '%s', available labels %s" % \
                        (label, [i.name for i in
                            self._categories[AnnotationType.label].items])
                    )

@attrs
class AnnotationMatcher:
    def match_annotations(self, sources):
        raise NotImplementedError()

@attrs
class LabelMatcher(AnnotationMatcher):
    @staticmethod
    def distance(a, b):
        return a.label == b.label

    def match_annotations(self, sources):
        return [sum(sources, [])]

@attrs(kw_only=True)
class _ShapeMatcher(AnnotationMatcher):
    pairwise_dist = attrib(converter=float, default=0.9)
    cluster_dist = attrib(converter=float, default=-1.0)

    def match_annotations(self, sources):
        distance = self.distance
        pairwise_dist = self.pairwise_dist
        cluster_dist = self.cluster_dist

        if cluster_dist < 0: cluster_dist = pairwise_dist

        id_segm = { id(a): (a, id(s)) for s in sources for a in s }

        def _is_close_enough(cluster, extra_id):
            # check if whole cluster IoU will not be broken
            # when this segment is added
            b = id_segm[extra_id][0]
            for a_id in cluster:
                a = id_segm[a_id][0]
                if distance(a, b) < cluster_dist:
                    return False
            return True

        def _has_same_source(cluster, extra_id):
            b = id_segm[extra_id][1]
            for a_id in cluster:
                a = id_segm[a_id][1]
                if a == b:
                    return True
            return False

        # match segments in sources, pairwise
        adjacent = { i: [] for i in id_segm } # id(sgm) -> [id(adj_sgm1), ...]
        for a_idx, src_a in enumerate(sources):
            for src_b in sources[a_idx+1 :]:
                matches, _, _, _ = match_segments(src_a, src_b,
                    dist_thresh=pairwise_dist, distance=distance)
                for m in matches:
                    adjacent[id(m[0])].append(id(m[1]))

        # join all segments into matching clusters
        clusters = []
        visited = set()
        for cluster_idx in adjacent:
            if cluster_idx in visited:
                continue

            cluster = set()
            to_visit = { cluster_idx }
            while to_visit:
                c = to_visit.pop()
                cluster.add(c)
                visited.add(c)

                for i in adjacent[c]:
                    if i in visited:
                        continue
                    if 0 < cluster_dist and not _is_close_enough(cluster, i):
                        continue
                    if _has_same_source(cluster, i):
                        continue

                    to_visit.add(i)

            clusters.append([id_segm[i][0] for i in cluster])

        return clusters

    @staticmethod
    def distance(a, b):
        return segment_iou(a, b)

@attrs
class BboxMatcher(_ShapeMatcher):
    pass

@attrs
class PolygonMatcher(_ShapeMatcher):
    pass

@attrs
class MaskMatcher(_ShapeMatcher):
    pass

@attrs(kw_only=True)
class PointsMatcher(_ShapeMatcher):
    sigma = attrib(converter=list, default=None)
    instance_map = attrib(converter=dict)

    def distance(self, a, b):
        a_bbox = self.instance_map[id(a)][1]
        b_bbox = self.instance_map[id(b)][1]
        if bbox_iou(a_bbox, b_bbox) <= 0:
            return 0
        bbox = mean_bbox([a_bbox, b_bbox])
        return OKS(a, b, sigma=self.sigma, bbox=bbox)

@attrs
class LineMatcher(_ShapeMatcher):
    @staticmethod
    def distance(a, b):
        a_bbox = a.get_bbox()
        b_bbox = b.get_bbox()
        bbox = max_bbox([a_bbox, b_bbox])
        area = bbox[2] * bbox[3]
        if not area:
            return 1

        # compute inter-line area, normalize by common bbox
        point_count = max(max(len(a.points) // 2, len(b.points) // 2), 5)
        a, sa = smooth_line(a.points, point_count)
        b, sb = smooth_line(b.points, point_count)
        dists = np.linalg.norm(a - b, axis=1)
        dists = (dists[:-1] + dists[1:]) * 0.5
        s = np.sum(dists) * 0.5 * (sa + sb) / area
        return abs(1 - s)

@attrs
class CaptionsMatcher(AnnotationMatcher):
    def match_annotations(self, sources):
        raise NotImplementedError()


@attrs(kw_only=True)
class AnnotationMerger:
    _context = attrib(type=IntersectMerge, default=None)

    def merge_clusters(self, clusters):
        raise NotImplementedError()

@attrs(kw_only=True)
class LabelMerger(AnnotationMerger, LabelMatcher):
    quorum = attrib(converter=int, default=0)

    def merge_clusters(self, clusters):
        assert len(clusters) <= 1
        if len(clusters) == 0:
            return []

        votes = {} # label -> score
        for label_ann in clusters[0]:
            votes[label_ann.label] = 1 + votes.get(label_ann.label, 0)

        merged = []
        for label, count in votes.items():
            if count < self.quorum:
                sources = set(self.get_ann_source(id(a)) for a in clusters[0]
                    if label not in [l.label for l in a])
                sources = [self._context._dataset_map[s][1] for s in sources]
                self._context.add_item_error(FailedLabelVotingError,
                    sources, votes)
                continue

            merged.append(Label(label, attributes={
                'score': count / len(self._context._dataset_map)
            }))

        return merged

@attrs(kw_only=True)
class _ShapeMerger(AnnotationMerger, _ShapeMatcher):
    quorum = attrib(converter=int, default=0)

    def merge_clusters(self, clusters):
        merged = []
        for cluster in clusters:
            label, label_score = self.find_cluster_label(cluster)
            shape, shape_score = self.merge_cluster_shape(cluster)

            shape.z_order = max(cluster, key=lambda a: a.z_order).z_order
            shape.label = label
            shape.attributes['score'] = label_score * shape_score \
                if label is not None else shape_score

            merged.append(shape)

        return merged

    def find_cluster_label(self, cluster):
        votes = {}
        for s in cluster:
            state = votes.setdefault(s.label, [0, 0])
            state[0] += s.attributes.get('score', 1.0)
            state[1] += 1

        label, (score, count) = max(votes.items(), key=lambda e: e[1][0])
        if count < self.quorum:
            self._context.add_item_error(FailedLabelVotingError, votes)
        score = score / count if count else None
        return label, score

    @staticmethod
    def _merge_cluster_shape_mean_box_nearest(cluster):
        mbbox = Bbox(*mean_bbox(cluster))
        dist = (segment_iou(mbbox, s) for s in cluster)
        nearest_pos, _ = max(enumerate(dist), key=lambda e: e[1])
        return cluster[nearest_pos]

    def merge_cluster_shape(self, cluster):
        shape = self._merge_cluster_shape_mean_box_nearest(cluster)
        shape_score = sum(max(0, self.distance(shape, s))
            for s in cluster) / len(cluster)
        return shape, shape_score

@attrs
class BboxMerger(_ShapeMerger, BboxMatcher):
    pass

@attrs
class PolygonMerger(_ShapeMerger, PolygonMatcher):
    pass

@attrs
class MaskMerger(_ShapeMerger, MaskMatcher):
    pass

@attrs
class PointsMerger(_ShapeMerger, PointsMatcher):
    pass

@attrs
class LineMerger(_ShapeMerger, LineMatcher):
    pass

@attrs
class CaptionsMerger(AnnotationMerger, CaptionsMatcher):
    pass

def match_segments(a_segms, b_segms, distance='iou', dist_thresh=1.0):
    if distance == 'iou':
        distance = segment_iou
    else:
        assert callable(distance)

    a_segms.sort(key=lambda ann: 1 - ann.attributes.get('score', 1))
    b_segms.sort(key=lambda ann: 1 - ann.attributes.get('score', 1))

    # a_matches: indices of b_segms matched to a bboxes
    # b_matches: indices of a_segms matched to b bboxes
    a_matches = -np.ones(len(a_segms), dtype=int)
    b_matches = -np.ones(len(b_segms), dtype=int)

    distances = np.array([[distance(a, b) for b in b_segms] for a in a_segms])

    # matches: boxes we succeeded to match completely
    # mispred: boxes we succeeded to match, having label mismatch
    matches = []
    mispred = []

    for a_idx, a_segm in enumerate(a_segms):
        if len(b_segms) == 0:
            break
        matched_b = a_matches[a_idx]
        max_dist = max(distances[a_idx, matched_b], dist_thresh)
        for b_idx, b_segm in enumerate(b_segms):
            if 0 <= b_matches[b_idx]: # assign a_segm with max conf
                continue
            d = distances[a_idx, b_idx]
            if d < max_dist:
                continue
            max_dist = d
            matched_b = b_idx

        if matched_b < 0:
            continue
        a_matches[a_idx] = matched_b
        b_matches[matched_b] = a_idx

        b_segm = b_segms[matched_b]

        if a_segm.label == b_segm.label:
            matches.append( (a_segm, b_segm) )
        else:
            mispred.append( (a_segm, b_segm) )

    # *_umatched: boxes of (*) we failed to match
    a_unmatched = [a_segms[i] for i, m in enumerate(a_matches) if m < 0]
    b_unmatched = [b_segms[i] for i, m in enumerate(b_matches) if m < 0]

    return matches, mispred, a_unmatched, b_unmatched

def mean_std(dataset):
    """
    Computes unbiased mean and std. dev. for dataset images, channel-wise.
    """
    # Use an online algorithm to:
    # - handle different image sizes
    # - avoid cancellation problem
    if len(dataset) == 0:
        return [0, 0, 0], [0, 0, 0]

    stats = np.empty((len(dataset), 2, 3), dtype=np.double)
    counts = np.empty(len(dataset), dtype=np.uint32)

    mean = lambda i, s: s[i][0]
    var = lambda i, s: s[i][1]

    for i, item in enumerate(dataset):
        counts[i] = np.prod(item.image.size)

        image = item.image.data
        if len(image.shape) == 2:
            image = image[:, :, np.newaxis]
        else:
            image = image[:, :, :3]
        # opencv is much faster than numpy here
        cv2.meanStdDev(image.astype(np.double) / 255,
            mean=mean(i, stats), stddev=var(i, stats))

    # make variance unbiased
    np.multiply(np.square(stats[:, 1]),
        (counts / (counts - 1))[:, np.newaxis],
        out=stats[:, 1])

    _, mean, var = StatsCounter().compute_stats(stats, counts, mean, var)
    return mean * 255, np.sqrt(var) * 255

class StatsCounter:
    # Implements online parallel computation of sample variance
    # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Parallel_algorithm

    # Needed do avoid catastrophic cancellation in floating point computations
    @staticmethod
    def pairwise_stats(count_a, mean_a, var_a, count_b, mean_b, var_b):
        delta = mean_b - mean_a
        m_a = var_a * (count_a - 1)
        m_b = var_b * (count_b - 1)
        M2 = m_a + m_b + delta ** 2 * count_a * count_b / (count_a + count_b)
        return (
            count_a + count_b,
            mean_a * 0.5 + mean_b * 0.5,
            M2 / (count_a + count_b - 1)
        )

    # stats = float array of shape N, 2 * d, d = dimensions of values
    # count = integer array of shape N
    # mean_accessor = function(idx, stats) to retrieve element mean
    # variance_accessor = function(idx, stats) to retrieve element variance
    # Recursively computes total count, mean and variance, does O(log(N)) calls
    @staticmethod
    def compute_stats(stats, counts, mean_accessor, variance_accessor):
        m = mean_accessor
        v = variance_accessor
        n = len(stats)
        if n == 1:
            return counts[0], m(0, stats), v(0, stats)
        if n == 2:
            return __class__.pairwise_stats(
                counts[0], m(0, stats), v(0, stats),
                counts[1], m(1, stats), v(1, stats)
                )
        h = n // 2
        return __class__.pairwise_stats(
            *__class__.compute_stats(stats[:h], counts[:h], m, v),
            *__class__.compute_stats(stats[h:], counts[h:], m, v)
            )

def compute_image_statistics(dataset):
    stats = {
        'dataset': {},
        'subsets': {}
    }

    def _extractor_stats(extractor):
        available = True
        for item in extractor:
            if not (item.has_image and item.image.has_data):
                available = False
                log.warn("Item %s has no image. Image stats won't be computed",
                    item.id)
                break

        stats = {
            'images count': len(extractor),
        }

        if available:
            mean, std = mean_std(extractor)
            stats.update({
                'image mean': [float(n) for n in mean[::-1]],
                'image std': [float(n) for n in std[::-1]],
            })
        else:
            stats.update({
                'image mean': 'n/a',
                'image std': 'n/a',
            })
        return stats

    stats['dataset'].update(_extractor_stats(dataset))

    subsets = dataset.subsets() or [None]
    if subsets and 0 < len([s for s in subsets if s]):
        for subset_name in subsets:
            stats['subsets'][subset_name] = _extractor_stats(
                dataset.get_subset(subset_name))

    return stats

def compute_ann_statistics(dataset):
    labels = dataset.categories().get(AnnotationType.label)
    def get_label(ann):
        return labels.items[ann.label].name if ann.label is not None else None

    stats = {
        'images count': len(dataset),
        'annotations count': 0,
        'unannotated images count': 0,
        'unannotated images': [],
        'annotations by type': { t.name: {
            'count': 0,
        } for t in AnnotationType },
        'annotations': {},
    }
    by_type = stats['annotations by type']

    attr_template = {
        'count': 0,
        'values count': 0,
        'values present': set(),
        'distribution': {}, # value -> (count, total%)
    }
    label_stat = {
        'count': 0,
        'distribution': { l.name: [0, 0] for l in labels.items
        }, # label -> (count, total%)

        'attributes': {},
    }
    stats['annotations']['labels'] = label_stat
    segm_stat = {
        'avg. area': 0,
        'area distribution': [], # a histogram with 10 bins
        # (min, min+10%), ..., (min+90%, max) -> (count, total%)

        'pixel distribution': { l.name: [0, 0] for l in labels.items
        }, # label -> (count, total%)
    }
    stats['annotations']['segments'] = segm_stat
    segm_areas = []
    pixel_dist = segm_stat['pixel distribution']
    total_pixels = 0

    for item in dataset:
        if len(item.annotations) == 0:
            stats['unannotated images'].append(item.id)
            continue

        for ann in item.annotations:
            by_type[ann.type.name]['count'] += 1

            if not hasattr(ann, 'label') or ann.label is None:
                continue

            if ann.type in {AnnotationType.mask,
                    AnnotationType.polygon, AnnotationType.bbox}:
                area = ann.get_area()
                segm_areas.append(area)
                pixel_dist[get_label(ann)][0] += int(area)

            label_stat['count'] += 1
            label_stat['distribution'][get_label(ann)][0] += 1

            for name, value in ann.attributes.items():
                if name.lower() in { 'occluded', 'visibility', 'score',
                        'id', 'track_id' }:
                    continue
                attrs_stat = label_stat['attributes'].setdefault(name,
                    deepcopy(attr_template))
                attrs_stat['count'] += 1
                attrs_stat['values present'].add(str(value))
                attrs_stat['distribution'] \
                    .setdefault(str(value), [0, 0])[0] += 1

    stats['annotations count'] = sum(t['count'] for t in
        stats['annotations by type'].values())
    stats['unannotated images count'] = len(stats['unannotated images'])

    for label_info in label_stat['distribution'].values():
        label_info[1] = label_info[0] / label_stat['count']

    for label_attr in label_stat['attributes'].values():
        label_attr['values count'] = len(label_attr['values present'])
        label_attr['values present'] = sorted(label_attr['values present'])
        for attr_info in label_attr['distribution'].values():
            attr_info[1] = attr_info[0] / label_attr['count']

    # numpy.sum might be faster, but could overflow with large datasets.
    # Python's int can transparently mutate to be of indefinite precision (long)
    total_pixels = sum(int(a) for a in segm_areas)

    segm_stat['avg. area'] = total_pixels / (len(segm_areas) or 1.0)

    for label_info in segm_stat['pixel distribution'].values():
        label_info[1] = label_info[0] / total_pixels

    if len(segm_areas) != 0:
        hist, bins = np.histogram(segm_areas)
        segm_stat['area distribution'] = [{
            'min': float(bin_min), 'max': float(bin_max),
            'count': int(c), 'percent': int(c) / len(segm_areas)
        } for c, (bin_min, bin_max) in zip(hist, zip(bins[:-1], bins[1:]))]

    return stats
