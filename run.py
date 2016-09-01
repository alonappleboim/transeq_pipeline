"""
This script performs the preprocessing steps and starts a dedicated process for every sample.
 """


import stat
from collections import Counter, OrderedDict
import getpass
import csv
import re
from secure_smtp import ThreadedTlsSMTPHandler
import logging as lg
import argparse
import os
import sys
import datetime
import multiprocessing as mp
import subprocess as sp
import shlex as sh
import shutil
import pickle

from config import *

if not sys.executable == INTERPRETER:  # divert to the "right" interpreter
    scriptpath = os.path.abspath(sys.modules[__name__].__file__)
    sp.Popen([INTERPRETER, scriptpath] + sys.argv[1:]).wait()
    exit()

from workers import *
from exporters import *
from analyzers import *
from utils import *
from filters import *


ERROR = -1
BEGIN = 0
FASTQ = 1
ALIGN = 2
COUNT = 3


USER_STATES = {'BEGIN':BEGIN, 'FASTQ':FASTQ, 'ALIGN':ALIGN, 'COUNT':COUNT}


class SampleManager(th.Thread):

    def __init__(self, sample, repq, logc, statc, work_manager, start_from, pargs):
        super(SampleManager, self).__init__()
        self.s = sample
        self.start_from = start_from
        self.repq = repq
        self.logc = logc
        self.statc = statc
        self.pargs = pargs
        self.wm = work_manager

    def collect_fastq(self):
        a = self.pargs
        self.logc.put((lg.DEBUG, 'collecting fastq for %s' % str(self.s)))
        pre1, pre2 = self.s.files['in1'], self.s.files['in2']
        if os.path.isfile(pre1) and os.path.isfile(pre2):
            cat = sp.Popen(['cat', pre1, pre2], stdout=sp.PIPE)
        elif os.path.isfile(pre1):
            cat = sp.Popen(['cat', pre1], stdout=sp.PIPE)
        elif os.path.isfile(pre2):
            cat = sp.Popen(['cat', pre2], stdout=sp.PIPE)
        else:
            cat = sp.Popen(['cat'], stdin=open(os.devnull), stdout=sp.PIPE)
        awk = sp.Popen(sh.split('''awk -F "\\t" '{print "@umi:"substr($4,%i,%i)"\\n"$3"\\n+\\n"$7}' '''
                                % (a.barcode_length+1, a.umi_length)), stdin=cat.stdout, stdout=sp.PIPE)
        gzip = sp.Popen(['gzip'], stdin=awk.stdout, stdout=open(self.s.files['fastq'], 'wb'))
        gzip.wait()
        if os.path.isfile(pre1): os.remove(pre1)
        if os.path.isfile(pre2): os.remove(pre2)
        self.logc.put((lg.DEBUG, '%s ready.' % str(self.s.files['fastq'])))

    def spikein_count(self):
        # align, and parse statistics
        # bowtie2 --local -p 4 -U {fastq.gz} -x {index} 2> {stats} >/dev/null
        a = self.pargs
        self.logc.put((lg.DEBUG, 'Aligning %s to spikein genome' % str(self.s)))
        args = (a.bowtie_exec, a.n_threads, self.s.files['fastq'], a.bowtie_spikein_index)
        bt = sp.Popen(sh.split('%s --local -p %i -U %s -x %s' % args),
                      stderr=sp.PIPE, stdout=open(os.devnull, 'w'))
        s = {k+'-spikein':v for k,v in parse_bowtie_stats(bt.stderr)}
        self.statc.put((self.s, 'stats', s))
        msg = '%i reads in %s aligned to spike-in genome uniquely' % (s['unique-align'], self.s)
        self.logc.put((lg.DEBUG, msg))

    def align(self):
        a = self.pargs
        self.logc.put((lg.DEBUG, 'Aligning %s to genome' % str(self.s)))
        files = self.s.files
        args = (a.bowtie_exec, a.n_threads, self.s.files['fastq'], a.bowtie_index)
        bt = sp.Popen(sh.split('%s --local -p %i -U %s -x %s' % args), stdout=sp.PIPE, stderr=sp.PIPE)
        awkcmd = ''.join(("""awk '{if (substr($1,1,1) == "@" && substr($2,1,2) == "SN")""",
                          """{print $0 > "%s";} print; }' """)) % files['sam_hdr']
        geth = sp.Popen(sh.split(awkcmd), stdin=bt.stdout, stdout=sp.PIPE)
        st = sp.Popen(sh.split('samtools view -b -o %s' % files['tmp_bam']), stdin=geth.stdout)
        st.wait()
        if 'unaligned_bam' in files:
            cmd = 'samtools view -f4 -b %s -o %s' % (files['tmp_bam'], files['unaligned_bam'])
            naligned = sp.Popen(sh.split(cmd), stdout=sp.PIPE)
            naligned.wait()
        aligned = sp.Popen(sh.split('samtools view -F4 -b %s' % files['tmp_bam']), stdout=sp.PIPE)
        sort = sp.Popen(sh.split('samtools sort -o %s -T %s' % (files['unfiltered_bam'], files['tmp_bam'])),
                        stdin=aligned.stdout)
        sort.wait()
        os.remove(files['tmp_bam'])
        s = parse_bowtie_stats(bt.stderr)
        n = self.fpipe.filter(files['unfiltered_bam'], files['bam'], files['sam_hdr'], files['bam_f'])
        os.remove(files['unfiltered_bam'])
        self.statc.put((self.s, 'stats', s))
        self.statc.put((self.s, 'stats', {'pass-filter':n}))
        msg = '%i reads in %s aligned to genome uniquely' % (s['unique-align'], self.s)
        self.logc.put((lg.DEBUG, msg))

    def make_track(self, strand, negate=None):
        self.logc.put((lg.DEBUG, 'Building track for %s strand of sample %s' % (strand, str(self.s))))
        if negate is None: negate = strand != 'w'
        files = self.s.files
        bedcmd = "bedtools genomecov -ibam %s -g %s -bg -strand %s"
        bed = sp.Popen(sh.split(bedcmd % (files['bam'], SGLP, STRANDS[strand])), stdout=sp.PIPE)
        if negate:
            bed = sp.Popen(['awk', '{print $1,$2,$3,"-"$4;}'], stdin=bed.stdout, stdout=sp.PIPE)
        sbed = sp.Popen(sh.split("sort -k1,1 -k2,2n"), stdin=bed.stdout, stdout=open(files['tmp_bed'], 'w'))
        sbed.wait()
        bw = sp.Popen([BG2W_EXEC, files['tmp_bed'], SGLP, self.s.files['bw'][strand]])
        bw.wait()
        os.remove(files['tmp_bed'])
        self.logc.put((lg.DEBUG, '%s ready' % self.s.files['bw'][strand]))

    def count(self):
        cnt = sp.Popen(sh.split('bedtools coverage -counts -a stdin -b %s' % self.s.files['bam']),
                       stdin=open(self.annot_file), stdout=sp.PIPE)
        cnt_dict = OrderedDict()
        with open(self.annot_file) as ttsf:
            for line in ttsf: cnt_dict[line.split('\t')[3]] = 0

        for line in buffered_lines(cnt.stdout):
            if not line: continue
            sline = line.strip().split('\t')
            cnt_dict[sline[3].strip()] = sline[6]
        cnt.wait()
        self.statc.put((self.s, 'tts', cnt_dict))

    def error(self, msg):
        self.repc.put((self.s.barcode, ERROR, msg))
        exit()

    def run(self):
        err, spikein = None, False
        cname = self.s.base_name() + '.channel'
        c = self.wm.get_channel(cname)
        if self.start_from <= BEGIN:
            self.wm.execute(self.collect_fastq, {}, cname)
            _, err = c.get() #blocking until done
            if err:
                msg = "could not collect FASTQ files for sample %s:\n%s" % (self.s.base_name(), err)
                self.error(msg)
            self.repc.put((self.s.barcode, BEGIN, None))
        if self.start_from <= ALIGN:
            if self.bowtie_spikein_index is not None:
                self.wm.execute(self.spikein_count, {}, cname)
                _, err = c.get()  # blocking until done
                msg = "error while aligning sample %s to spikein:\n%s" % (self.s.base_name(), err)
                self.error(msg)
            self.wm.execute(self.align, {}, cname)
            _, err = c.get()
            if err:
                msg = "error while aligning sample %s:\n%s" % (self.s.base_name(), err)
                self.error(msg)
            self.repc.put((self.s.barcode, ALIGN, None))
        elif self.start_from <= COUNT:
            self.wm.execute(self.make_track, dict(strand='w'), cname)
            _, err = c.get()
            if err:
                msg = 'Error while building tracks for sample %s:\n%s' % (self.s.base_name(), err)
                self.error(msg)
            self.wm.execute(self.make_track, dict(strand='c'), cname)
            _, err = c.get()
            if err:
                msg = 'Error while building tracks for sample %s:\n%s' % (self.s.base_name(), err)
                self.error(msg)
            self.wm.execute(self.count, {}, cname)
            _, err = q.get()
            if err:
                msg = 'Error while counting sample %s:\n%s' % (self.s.base_name(), err)
                self.error(msg)
        self.repc.put((self.s.barcode, COUNT, None))


class MainHandler(object):

    @staticmethod
    def log(logc, logger):
        for lvl, msg in iter(logc.get, None): logger.log(lvl, msg)

    @staticmethod
    def collect_stats(statc):
        counters = {}
        for sample, stype, cnt in iter(statc.get, None):
            if type not in counters: counters[stype] = {}
            if sample not in counters[type]: counters[stype][sample] = Counter()
            counters[stype][sample].update(cnt)
        statc.put(counters)

    def setup_log(self):
        logger = lg.getLogger()
        logfile = get_logfile()
        logger.setLevel(lg.DEBUG)
        fh = lg.FileHandler(logfile)  # copy log to the log folder when done.
        fh.formatter = lg.Formatter('%(levelname)s\t%(asctime)s\t%(message)s')
        ch = lg.StreamHandler()
        ch.formatter = lg.Formatter('%(asctime)-15s\t%(message)s')
        fh.setLevel(lg.DEBUG)
        ch.setLevel(lg.INFO)
        if self.debug is not None: ch.setLevel(lg.DEBUG)
        if self.user_emails is not None:
            mailh = ThreadedTlsSMTPHandler(mailhost=('smtp.gmail.com', 587),
                                           fromaddr='transeq.pipeline@google.com',
                                           toaddrs=self.user_emails.split(','),
                                           credentials=('transeq.pipeline', 'transeq1234'),
                                           subject='TRANSEQ pipeline message')
            mailh.setLevel(lg.CRITICAL)
            logger.addHandler(mailh)
        logger.addHandler(fh)
        logger.addHandler(ch)
        self.logger = th.Thread(target=MainHandler.log, args=(self.logc, logger))
        self.logger.start()
        self.logfile = logfile

    def __init__(self, argobj, cmdline):
        self.__dict__.update(argobj.__dict__)
        self.pargs = argobj
        self.wm = WorkManager(self.exec_on == 'slurm', delay=self.delay, max_w=self.n_workers)

        self.logc = self.wm.get_channel('log')
        self.setup_log()
        self.logc.put((lg.INFO, 'commandline: %s' % cmdline))
        self.repc = self.wm.get_channel('report')
        if self.debug:
            self.logc.put((lg.INFO, '=================== DEBUG MODE (%s) ===================' % self.debug))
        self.check_third_party()

        self.bc_len, self.samples, self.features = self.parse_sample_db()
        self.generate_dir_tree()
        sfname = self.output_dir + os.sep + 'sample_db.csv'
        if not os.path.isfile(sfname): shutil.copy(self.sample_db, sfname)

        self.statc = self.wm.get_channel('statc')
        self.statc = th.Thread(target=MainHandler.collect_stats, args=(self.statc,))
        self.statc.start()

        self.fpipe = build_filter_schemes('filter:'+self.filter)['filter']
        self.logc.put((lg.INFO, 'Filters:\n' + str(self.fpipe)))
        self.exporters = exporters_from_string(self.exporters, self.output_dir)

    def execute(self):
        sps = []
        for s in self.samples.values():
            sm = SampleManager(s, self.repc, self.logc, self.statc, self.wm,
                               self.start_from, self.pargs)
            sm.start()
            sps.append(sm)
        status = {ERROR:{}, FASTQ:{}, ALIGN:{}, COUNT:{}}
        while len(status[COUNT])+len(status[ERROR]) < len(self.samples):
            while True:
                try:
                    bc, type, msg = self.repc.get(timeout=self.delay)
                    s = self.samples[bc]
                    status[type][s] = msg
                    if type == ERROR: self.logc.put((lg.ERROR, msg))
                    if len(status[FASTQ]) == len(self.samples): self.checkpoint(FASTQ)
                    if len(status[ALIGN]) == len(self.samples): self.checkpoint(ALIGN)
                except Empty: break
        self.aftermath()

    def print_stats(self):
        stats = set([])
        fns = ['sample'] + self.stat_order
        fh = open(self.output_dir + os.sep + self.exp + '_statistics.csv','w')
        wrtr = csv.DictWriter(fh, fns)
        wrtr.writeheader()
        for s, st in self.stats.items():
            wrtr.writerow(dict(sample=s, **{k:str(st[k]) for k in fns[1:]}))
        fh.close()

    def checkpoint(self, stage):
        self.print_stats()
        stage = STATES[stage]
        msg = ('Finished stage %s. You can continue the pipeline from this point '
               'with the option -sa %s (--start_from %s)' % (stage, stage, stage))
        self.logc.put((lg.INFO, msg))
        self.copy_log()
        fh = open(self.output_dir + os.sep + '.pipeline_state', 'w')
        fh.write('%s\n' % stage)
        fh.close()

    def get_mark(self):
        with open(self.output_dir + os.sep + '.pipeline_state') as fh:
            return fh.read().strip()

    def copy_log(self):
        shutil.copy(self.logfile, self.output_dir + os.sep + 'full.log')

    def check_third_party(self):
        if self.exec_on == 'slurm':
            try:
                p = sp.Popen(sh.split('srun "slurm, are you there?"'), stdout=sp.PIPE, stderr=sp.PIPE)
                p.communicate()
                self.logc.put((lg.INFO, "slurm check.. OK"))
            except OSError as e:
                self.logc.put((lg.CRITICAL, "This is not a slurm cluster, execute with flag -eo=local"))
                raise e

        for ex in [k for k in self.__dict__.keys() if k.endswith('exec')]:
            try:
                p = sp.Popen(sh.split('%s --help' % self.__dict__[ex]), stdout=sp.PIPE, stderr=sp.PIPE)
                p.communicate()
                self.logc.put((lg.INFO, "%s check.. OK" % ex))
            except OSError as e:
                self.logc.put((lg.CRITICAL, "could not resolve %s path: %s" % (ex, self[ex])))
                raise e

    def parse_sample_db(self):

        def parse_features(hdr):
            feat_pat = re.compile('\s*(?P<name>\w+)\s*(?:\((?P<short_name>\w+)\))?'
                                  '\s*:(?P<type>\w+)(:?\[(?P<units>\w+)\])?')
            hdr = hdr.split(DELIM)
            features = FeatureCollection()
            f_pos_map = {}
            for i, f in enumerate(hdr):
                if i == 0:
                    assert hdr[0] == 'barcode', 'first column in sample db needs to be the "barcode" column'
                elif f.startswith('#'):
                    msg = ("ignoring column %s in sample db" % f)
                    self.logc.put((lg.INFO, msg))
                else:
                    m = re.match(feat_pat, f)
                    if m is None:
                        msg = ("couldn't understand feature '%s' in sample_db file, format should be: "
                               "<name>(<short_name>):(str|int|float)[units] (short_name and units are optional) or "
                               "column is ignored if it starts with '#'" % f)
                        self.logc.put((lg.CRITICAL, msg))
                        raise (ValueError(msg))
                    try:
                        f_pos_map[i] = Feature(**m.groupdict())
                    except ValueError:
                        snames = '\n'.join(f.short_name for f in features.values)
                        msg = ("features must have distinct names and short_names - %s appears at least twice (or "
                               "its short_name matched a previous generated short name):\n%s" % f, snames)
                        self.logc.put((lg.CRITICAL, msg))
                        raise (ValueError(msg))
                    features.add_feature(f_pos_map[i])
            return features, f_pos_map

        def parse_samples(file, f_pos_map):
            b2s, bc_len = OrderedDict(), None
            for i, line in enumerate(file):
                if line.strip()[0] == '#': continue  # comment
                if self.debug is not None:
                    if i >= self.db_nsamples: break  # limit number of samples
                sample = Sample()
                for j, val in enumerate(line.strip().split(DELIM)):
                    val = val.strip()
                    if j == 0:
                        if i == 0:
                            bc_len = len(val)  # first barcode
                        elif bc_len != len(val):
                            msg = "barcode %s has a different length" % val
                            self.logc.put((lg.CRITICAL, msg))
                            raise (TypeError(msg))
                        if val in b2s:
                            msg = "barcode %s is not unique" % val
                            self.logc.put((lg.CRITICAL, msg))
                            raise (TypeError(msg))
                        sample.barcode = val
                    elif j in f_pos_map:
                        f = f_pos_map[j]
                        try:
                            v = f.type(val)
                        except ValueError:
                            msg = ("couldn't cast value %s in sample %i, feature '%s' to "
                                   "given type - %s." % (val, i + 1, f.name, f.strtype))
                            self.logc.put((lg.CRITICAL, msg))
                            raise (ValueError(msg))
                        f.vals.add(v)
                        sample.fvals[f] = v
                    if hash(sample) in [hash(s) for s in b2s.values()]:
                        msg = "2 samples (or more) seem to be identical - %s" % sample
                        self.logc.put((lg.CRITICAL, msg))
                        raise (TypeError(msg))
                b2s[sample.barcode] = sample
            return b2s, bc_len

        sdb = open(self.sample_db)
        exp = re.match('.*experiment.*:\s+(\w+)', sdb.readline())
        if exp is None:
            msg = 'barcodes file should contain a header with experiment name: ' \
                  'experiment: <expname>'
            self.logc.put((lg.CRITICAL, msg))
            raise ValueError(msg)
        self.user = getpass.getuser()
        self.exp = exp.group(1)
        msg = 'user: %s, experiment: %s' % (self.user, self.exp)
        self.logc.put((lg.INFO, msg))
        features, f_pos_map = parse_features(sdb.readline())
        b2s, bc_len = parse_samples(sdb, f_pos_map)
        sdb.close()
        msg = '\n'.join(['barcodes:'] + ['%s -> %s' % (b, s.base_name()) for b, s in b2s.items()])
        self.logc.put((lg.DEBUG, msg))
        self.logc.put((lg.INFO, 'found %i samples.' % len(b2s)))
        msg = 'features:\n' + '\n'.join('%s: %s' % (str(f), ','.join(str(x) for x in f.vals)) for f in features.values())
        self.logc.put((lg.DEBUG, msg))

        return bc_len, b2s, features

    def collect_input_fastqs(self):
        files = {}
        for fn in os.listdir(self.fastq_path):
            if not fn.endswith('fastq.gz'): continue
            if not fn.startswith(self.fastq_pref): continue
            parts = re.split('_R\d',fn)
            if len(parts) == 2:
                path = self.fastq_path + os.sep + fn
                pref = parts[0]
                if pref in files:
                    if '_R1' in files[pref]:
                        files[pref] = (files[pref], path)
                    else:
                        files[pref] = (path, files[pref])
                else:
                    files[pref] = path

        files = [f for f in files.values() if type(()) == type(f)]
        msg = '\n'.join('found fastq files:\n%s\n%s' % fs for fs in files)
        self.logc.put((lg.INFO, msg))
        if not files:
            msg = "could not find R1/R2 fastq.gz pairs in given folder: %s" % self.fastq_path
            self.logc.put((lg.CRITICAL, msg))
            raise IOError(msg)
        self.input_files = files

    def generate_dir_tree(self):
        if self.start_from != BEGIN:
            try:
                cur = self.get_mark()
                if USER_STATES[cur] >= self.start_from:
                    sf = [k for k,v in USER_STATES.items() if v==self.start_from][0]
                    msg = 'restarting from %s in folder: %s ' % (sf, self.output_dir)
                    self.logc.put((lg.INFO, msg))
                else:
                    msg = 'folder state %s in folder %s incompatible with --start_from %s request' \
                          % (cur, self.output_dir, self.start_from)
                    self.logc.put((lg.CRITICAL, msg))
                    exit()
            except IOError:
                msg = 'could not find an existing output folder: %s' % self.output_dir
                self.logc.put((lg.CRITICAL, msg))
                exit()
        else:
            if self.output_dir is None:
                folder = canonic_path(DATA_PATH) + os.sep + self.user
                create_dir(folder)
                folder += os.sep + self.exp
                create_dir(folder)
                folder += os.sep + datetime.datetime.now().strftime("%d-%m-%y")
                create_dir(folder)
                if self.debug is not None:
                    fname = 'debug'
                else:
                    i = [int(x) for x in os.listdir(folder) if isint(x)]
                    if not i: i = 1
                    else: i = max(i) + 1
                    fname = str(i)
                folder += os.sep + fname
                self.output_dir = folder

        d = self.output_dir
        self.tmp_dir = d + os.sep + TMP_NAME
        self.fastq_dir = d + os.sep + self.fastq_dirname
        self.bw_dir = d + os.sep + self.bigwig_dirname
        self.bam_dir = d + os.sep + self.bam_dirname
        if os.path.isdir(self.tmp_dir): shutil.rmtree(self.tmp_dir)

        if self.start_from == BEGIN:
            # assuming all folder structure exists if check passes
            self.create_dir_and_log(d, lg.INFO)
            self.create_dir_and_log(self.fastq_dir)
            self.create_dir_and_log(self.bam_dir)
            self.create_dir_and_log(self.bw_dir)
            if self.keep_filtered:
                self.filtered_dir = d + os.sep + FILTERED_NAME
                self.create_dir_and_log(self.filtered_dir)
            if self.keep_unaligned:
                self.unaligned_dir = d + os.sep + UNALIGNED_NAME
                self.create_dir_and_log(self.unaligned_dir)

        self.create_dir_and_log(self.tmp_dir)

    def split_barcodes(self, no_bc=None):
        #
        #  first - compile awk scripts that will split input according to barcodes,
        # then use them with some smart pasting. For each input pair:
        # paste <(zcat {R1}) <(zcat {R2}) |\ %paste both files one next to the other
        # paste - - - -|\ % make every 4 lines into one
        # awk -F "\\t" -f {script1-exact_match}' |\ % split exact barcode matches
        # awk -F "\\t" -f {script2-ham_dist}' % split erroneous matches (subject to given hamming distance)
        #
        """
        :param no_bc: if given, orphan fastq enries are written to this prefix (with R1/R2 interleaved)
        """
        def compile_awk(it, b2s):
            cnt_path = self.tmp_dir + os.sep + (BC_COUNTS_FNAME % it)
            nobc = NO_BC_NAME + '-' + it
            arraydef = ';\n'.join('a["%s"]="%s-%s"' % (b, s, it) for b, s in b2s.items()) + ';\n'
            awk_str = (""" 'BEGIN {%s} {x=substr($4,1,%i); if (x in a) """,
                       """{c[a[x]]++; print >> "%s/"a[x];} else {c["%s"]++; print;} }""",
                       """END { for (bc in c) print bc, c[bc] >> "%s" } '""")
            awk_str = ''.join(awk_str) % (arraydef, self.bc_len , self.tmp_dir, nobc, cnt_path)
            return awk_str, cnt_path

        def merge_statistics(bc1, bc2):
            stat = "n_reads"
            self.stat_order.append(stat)
            self.stats[NO_BC_NAME] = Counter()
            with open(bc1) as IN:
                for line in IN:
                    sample, cnt = line.strip().split(' ')
                    sample = sample[:-2]
                    if sample == 'no-barcode': continue  # only from bc_counts-2
                    self.stats[sample][stat] += int(cnt)
            os.remove(bc1)
            with open(bc2) as IN:
                for line in IN:
                    sample, cnt = line.strip().split(' ')
                    self.stats[sample[:-2]][stat] += int(cnt)
            os.remove(bc2)
            for s in self.samples.values():
                if s.base_name() not in self.stats:
                    self.stats[s.base_name()][stat] += 0
            msg = '\n'.join(['%s: %i' % (s, c[stat]) for s, c in self.stats.items()])
            self.logc.put((lg.CRITICAL, 'read counts:\n' + msg))

        hb = {}
        for b,s in self.samples.items():
            hb.update({eb:s.base_name() for eb in hamming_ball(b, self.hamming_distance)})

        awk1p, cnt1 = compile_awk("1", {b: s.base_name() for b,s in self.samples.items()})
        awk2p, cnt2 = compile_awk("2", hb)
        outf = open(os.devnull, 'w') if no_bc is None else open(no_bc, 'wb')
        for r1, r2 in self.input_files:
            msg = 'splitting files:\n%s\n%s' % (os.path.split(r1)[1],os.path.split(r2)[1])
            self.logc.put((lg.INFO, msg))
            paste1 = sp.Popen('paste <(zcat %s) <(zcat %s)' % (r1,r2), stdout=sp.PIPE,
                              shell=True, executable='/bin/bash')
            awkin = sp.Popen(sh.split('paste - - - -'), stdin=paste1.stdout, stdout=sp.PIPE)
            if self.debug: # only a small subset of reads
                nlines = round(self.db_nlines/4)*4  # making sure it's in fastq units
                awkin = sp.Popen(sh.split('head -%i' % nlines), stdin=awkin.stdout, stdout=sp.PIPE)
            awk1 = sp.Popen(sh.split('awk -F "\\t" ' + awk1p), stdin=awkin.stdout, stdout=sp.PIPE)
            awk2 = sp.Popen(sh.split('awk -F "\\t" ' + awk2p), stdin=awk1.stdout, stdout=sp.PIPE)
            awkcmd = """awk -F "\\t" '{print $1"\\n"$3"\\n"$5"\\n"$7; print $2"\\n"4"\\n"$6"\\n"$8;}' """
            wfastq = sp.Popen(sh.split(awkcmd), stdin=awk2.stdout, stdout=sp.PIPE)
            gzip = sp.Popen(['gzip'], stdin=wfastq.stdout, stdout=outf)
            wfastq.wait() # to prevent data interleaving
        gzip.wait()
        self.logc.put((lg.INFO, 'Barcode splitting finished.'))

        merge_statistics(cnt1, cnt2)

    def aftermath(self):
        # remove temp folder
        # modify file permissions for the entire tree
        # make everything read only (optional?)
        # store pipeline code?
        # merge and report statistics
        # merge data to single (usable) files
        if self.debug is None:
            shutil.rmtree(self.tmp_dir)

        print('xxx')
        pickle.dump(self.pargs, open(self.output_dir + os.sep + 'args.pkl', 'wb'))

        # change permissions so everyone can read into folder
        for d, _, fs in os.walk(self.output_dir):
            st = os.stat(d)
            os.chmod(d, st.st_mode | stat.S_IRGRP | stat.S_IXGRP)

        self.logc.put((lg.CRITICAL, 'All done.'))
        self.copy_log()

    def create_dir_and_log(self, path, level=lg.DEBUG):
        create_dir(path)
        self.logc.put((level, 'creating folder %s' % path))

    def add_hub(self):
        pass
        # track_db = open(
        # for (hf, hr), p, hd in zip(handles, paths, hdr):
        #     hd + '_F', URL + hd + '_F.bw', 'pA - %s (F)' % hd, 'Wilkening 2013 polyA data, %s (forward)' % hd)
        #     trackfile.write("track %s\n"
        #                     "bigDataUrl %s\n"
        #                     "shortLabel %s\n"
        #                     "longLabel %s\n"
        #                     "type bigWig\n"
        #                     "visibility full\n"
        #                     "viewLimits 0:500\n\n" % entries)
        #     entries = (
        #     hd + '_R', URL + hd + '_R.bw', 'pA - %s (R)' % hd, 'Wilkening 2013 polyA data, %s (reverse)' % hd)
        #     trackfile.write("track %s\n"
        #                     "bigDataUrl %s\n"
        #                     "shortLabel %s\n"
        #                     "longLabel %s\n"
        #                     "type bigWig\n"
        #                     "visibility full\n"
        #                     "viewLimits -500:0\n\n" % entries)
        # hubfile = open(hub_path + os.path.sep + 'hub.txt', 'wb')
        # hubfile.write("hub %s\n"
        #               "shortLabel pA sites\n"
        #               "longLabel Data relevant to 3' processing and alternative UTRs\n"
        #               "genomesFile genomes.txt\n"
        #               "email  alonappleboim@gmail.com" % os.path.split(hub_path)[-1])
        # genomesfile = open(hub_path + os.path.sep + 'genomes.txt', 'wb')
        # genomesfile.write("genome sacCer3\n"
        #                   "trackDb sacCer3%strackDB.txt" % os.path.sep)


def build_parser():
    p = argparse.ArgumentParser()

    g = p.add_argument_group('Input and performance')
    g.add_argument('--fastq_prefix', '-fp', type=str, default=None,
                   help='path to a prefix of fastq files (R1 & R2) containing the transeq data.'
                        'This can be a folder (must end with "/"), in which case all R1/R2 pairs'
                        'in the folder are considered, or a "path/to/files/prefix", in which case '
                        'all files in the path with the prefix are considered')
    g.add_argument('--start_from', '-sf', default='BEGIN',
                   choices=[k for k in USER_STATES.keys()],
                   help='If given the pipeline will try to continue a previous run, specified through '
                        'the "output_dir" argument, from the selected stage. In this case --fastq_prefix'
                        ' is ignored.')
    g.add_argument('--n_workers', '-nw', default=50, type=int,
                   help='maximal number of parallel native processes used by the pipeline')
    g.add_argument('--delay', default=.1, type=float,
                   help='all polling loops in the pipeline use this delay (in seconds) between '
                        'iterations')

    g = p.add_argument_group('Output')
    g.add_argument('--output_dir', '-od', default=None, type=str,
                   help='path to the folder in which most files are written. '
                        'If not given, the date and info from barcode file are used to '
                        'generate a new folder in %s' % DATA_PATH)
    g.add_argument('--fastq_dirname', '-fd', default='FASTQ', type=str,
                   help='name of folder in which fastq files are written. relative to output_dir')
    g.add_argument('--bam_dirname', '-bd', default='BAM', type=str,
                   help='name of folder in which bam files are written relative to output_dir')
    g.add_argument('--bigwig_dirname', '-bwd', default='BIGWIG', type=str,
                   help='name of folder in which bigwig files are written relative to output_dir')
    g.add_argument('--user_emails', '-ue', default=None, type=str,
                   help="if provided these comma separated emails will receive notifications of ctitical "
                        "events (checkpoints, fatal errors, etc.)")
    g.add_argument('--debug', '-d', default=None, type=str,
                   help='Highly recommended. Use this mode with a pair of comma separated integers:'
                        '<numlines>,<numsamples>. The pipeline will extract this number of lines from '
                        'every input file pair, and will only run with this number of samples out of the '
                        'given barcode file. If output folder (-od) is not given, results are written to '
                        'a "debug" folder.')

    g = p.add_argument_group('Barcode Splitting')
    g.add_argument('--sample_db', '-sd', type=str, default=None,
                   help='a file with an experiment name in first row \n experiment: <expname> followed by a'
                        ' header whose first column is "barcode", and the remaining entries are the features '
                        'present in the experiment: <name>(short_name):(str|int|float)[units], with the '
                        'units and short_name being optional. The remaining lines define the experiment samples '
                        'according to the header. Default is "sample_db.csv" in the folder of the --fastq_prefix input')
    g.add_argument('--umi_length', '-ul', type=int, default=8,
                   help='UMI length')
    g.add_argument('--hamming_distance', '-hd', default=1, type=int,
                   help='barcode upto this hamming distance from given barcodes are handled by '
                        'the pipeline')
    g.add_argument('--keep_nobarcode', '-knb', action='store_true',
                   help='keep reads that did not match any barcode in a fastq file. Notice that the '
                        'R1/R2 reads are interleaved in this file.')

    g = p.add_argument_group('Alignment')
    g.add_argument('--bowtie_index', '--bi', type=str, default='/cs/wetlab/genomics/scer/bowtie/sacCer3',
                   help='path prefix of genome bowtie index')
    g.add_argument('--spikein_index_path', '-sip', type=str, default=None,
                   help='If given, data is also aligned to this genome (only counts reported, k.lactis '
                        'can be found at /cs/wetlab/genomics/klac/bowtie/genome)')
    g.add_argument('--n_threads', '-an', type=int, default=4,
                   help='number of threads used for alignment per bowtie instance')
    g.add_argument('--keep_unaligned', '-ku', action='store_true',
                   help='if set, unaligned reads are written to '
                        'output_folder/%s/<sample_name>.bam' % UNALIGNED_NAME)

    g = p.add_argument_group('Filter',
                             description='different filters applied to base BAM file. Only reads that pass all filters '
                                         'are passed on')
    g.add_argument('--filter', '-F', action='store',
                   default='dup(),qual()',
                   help='specify a filter scheme to apply to data. Expected string conforms to:\n' \
                        '[<filter_name>([<argname1=argval1>,]+)[+|-]);]*\n. use "run -fh" for more info. '
                        'default = "dup(),qual()"')
    g.add_argument('--keep_filtered', '-kf', action='store_true',
                   help='if set, filtered reads are written to '
                        'output_folder/%s/<sample_name>.bam' % FILTERED_NAME)
    g.add_argument('--filter_specs', '-fh', action='store_true',
                   help='print available filters, filter help and exit')

    g = p.add_argument_group('Execution and third party')
    g.add_argument('--exec_on', '-eo', choices=['slurm', 'local'], default='local', #TODO implement slurm...
                   help='whether to to submit tasks to a slurm cluster (default), or just use the local processors')
    g.add_argument('--bowtie_exec', '-bx', type=str, default=BOWTIE_EXEC,
                   help='full path to bowtie executable')
    g.add_argument('--samtools_exec', '-sx', type=str, default=SAMTOOLS_EXEC,
                   help='full path to samtools executable')

    g = p.add_argument_group('Tracks')
    g.add_argument('--www_path', '-wp', default= '~/www',
                   help='in this path a hub folder is generated, that contains symbolic links to all bigwig files'
                        'and a hub definition. The hub URL will be verified and produced.')

    g = p.add_argument_group('Count')
    g.add_argument('--tts_file', '-tf', default=TTS_MAP,
                   help='annotations for counting. Expected format is a tab delimited file with "chr", "ACC", "start",'
                        '"end", and "TTS" columns. default is found at %s' % TTS_MAP)
    g.add_argument('--count_window', '-cw', type=str, default='[-500,100]',
                   help='Comma separated limits for tts counting, relative to annotation TTS. default=-100,500')

    g = p.add_argument_group('Export')
    g.add_argument('--exporters', '-E', action='store',
                   default='tab();mat(r=True)',
                   help='specify a exporters for the pipeline data and statistics. default = "tab();mat(r=True)"')
    g.add_argument('--export_path', '-ep', default=None,
                   help='if given, exported data is copied to this path as well')
    g.add_argument('--exporter_specs', '-eh', action='store_true',
                   help='print available exporters, exporter help and exit')
    return p


def pprint_class_with_args_dict(dict):
    slist = []
    for cls in dict.values():
        args = cls.__dict__['args']
        slist.append('Name: %s' % cls.__dict__['name'])
        for a, v in cls.__dict__.items():
            if a.startswith('__'): continue
            if hasattr(v, '__call__'): continue
            if a not in ['args', 'name']:
                slist.append('\t%s: %s' % (str(a),str(v)))
        slist.append('\targuments:')
        for name, (type, default, desc) in args.items():
            t = 'int' if type is int else 'float' if type is float else 'bool' if type is bool else 'str'
            slist.append('\t\t%s(%s):, default: %s, description: %s' % (name, t, str(default), desc))
    return '\n'.join(slist)


def parse_args(p):
    """
    :param p: argument parser
    :return: the arguments parsed, after applying all argument logic and conversion
    """
    args = p.parse_args()

    if args.filter_specs:
        h = ('Any collection of filters can be applied. The only reads that are written to the BAM '
             'folder are the ones that pass the complete list of filters. If you want to keep filtered '
             'reads for inspection, use the --keep_filtered options, and they will be written to the '
             '%s folder. Note that the reads are written by each filter separately, so any read that was '
             'filtered by multiple filters will be written more than once. \n'
             'A collection of filters is given as a comma seprated list of filter calls - a filter '
             'name followed by parentheses with optional argument setting within. The parentheses are followed '
             'by an optional +|- sign, to negate the filter ("-"). The default is "+". For example:\n'
             '"dup(kind=start&umi),polya(n=5)" will only keep reads that are unique when considering only read '
             'start position and the umi, and are considered polya reads with more than 5 A/T. See all available '
             'filters below.\n') % (FILTERED_NAME,)
        spec = pprint_class_with_args_dict(collect_filters())
        print('\n========= FILTER HELP and SPECIFICATIONS =========\n')
        print(h)
        print(spec)
        exit()

    if args.exporter_specs:
        h = ('Any collection of exporters can be given. Exporters format the quantiative output of the piplines '
             'for downstream analysis. Specifically - the statistics and TTS read counts. In addition,'
             'using the --export_path option will copy the outputs to the requested folder with a prefix of the '
             'experiment name.\n'
             'A collection of exporters is given as a semicolon-separated list of exporter calls - an exporter name '
             'followed by parentheses with optional argument setting within. For example:\n'
             '"tab(),mat(r=True)" export all files in a tab delimited format and a matlab format with table reshaping'
             ' according to experimental features.')
        spec = pprint_class_with_args_dict(collect_exporters())
        print('\n========= EXPORTER HELP and SPECIFICATIONS =========\n')
        print(h)
        print(spec)
        exit()

    args.__dict__['start_from'] = USER_STATES[args.start_from]
    if args.start_from != BEGIN:
        if args.output_dir is None:
            print('If the --start_from option is used, an existing output directory must be provided (-od).')
            exit()
        args.__dict__['fastq_prefix'] = args.output_dir  # ignoring input folder
    else:
        if args.fastq_prefix is None:
            print('If the --start_from option is not used, an input '
                  'fastq prefix/folder must be provided (--fastq_prefix).')
            exit()
    p, s = os.path.split(args.fastq_prefix)
    args.__dict__['fastq_path'] = p
    args.__dict__['fastq_pref'] = s
    args.__dict__['fastq_prefix'] = canonic_path(args.fastq_prefix)
    p, s = os.path.split(args.fastq_prefix)

    if args.sample_db is None:
        args.__dict__['sample_db'] = os.sep.join([args.fastq_path, 'sample_db.csv'])

    if args.debug is not None:
        nlines, nsamples = args.debug.split(',')
        args.__dict__['db_nlines'] = int(nlines)
        args.__dict__['db_nsamples'] = int(nsamples)

    if args.output_dir is not None:
        args.__dict__['output_dir'] = canonic_path(args.__dict__['output_dir'])

    if args.export_path is not None:
        args.__dict__['export_path'] = canonic_path(args.export_path)

    args.__dict__['count_window'] = [int(x) for x in args.count_window[1:-1].split(',')]
    return args


if __name__ == '__main__':
    p = build_parser()
    a = parse_args(p)
    mh = MainHandler(a, ' '.join(sys.argv)) #also print git current version from ./.git/log/HEAD,last row, 2nd column
    mh.execute()