import logging
from threading import Lock

from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _

from .result import BallotSet

logger = logging.getLogger(__name__)


class ScoreField(models.FloatField):
    pass


class Submission(models.Model):
    """Abstract base class to provide functionality common to different
    types of submissions.

    The unique_together class attribute of the Meta class MUST be set in
    all subclasses."""

    SUBMITTER_TABROOM = 'T'
    SUBMITTER_PUBLIC = 'P'
    SUBMITTER_TYPE_CHOICES = ((SUBMITTER_TABROOM, _("Tab room")),
                              (SUBMITTER_PUBLIC, _("Public")), )

    timestamp = models.DateTimeField(auto_now_add=True,
        verbose_name=_("timestamp"))
    version = models.PositiveIntegerField(
        verbose_name=_("version"))
    submitter_type = models.CharField(max_length=1, choices=SUBMITTER_TYPE_CHOICES,
        verbose_name=_("submitter type"))
    confirmed = models.BooleanField(default=False,
        verbose_name=_("confirmed"))

    # only relevant if submitter was in tab room
    submitter = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE,
        blank=True, null=True, related_name="%(app_label)s_%(class)s_submitted",
        verbose_name=_("submitter"))
    confirmer = models.ForeignKey(settings.AUTH_USER_MODEL, models.CASCADE,
        blank=True, null=True, related_name="%(app_label)s_%(class)s_confirmed",
        verbose_name=_("confirmer"))
    confirm_timestamp = models.DateTimeField(blank=True, null=True,
        verbose_name=_("confirm timestamp"))
    ip_address = models.GenericIPAddressField(blank=True, null=True,
        verbose_name=_("IP address"))

    version_lock = Lock()

    class Meta:
        abstract = True

    @property
    def _unique_filter_args(self):
        return dict((arg, getattr(self, arg)) for arg in self._meta.unique_together[0]
                    if arg != 'version')

    def save(self, *args, **kwargs):
        # Check for uniqueness.
        if self.confirmed:
            try:
                current = self.__class__.objects.get(confirmed=True, **self._unique_filter_args)
            except self.DoesNotExist:
                pass
            else:
                if current != self:
                    logger.warning("{} confirmed while {} was already confirmed, setting latter "
                            "to unconfirmed".format(self, current))
                    current.confirmed = False
                    current.save()

        # Assign the version field to one more than the current maximum version.
        # Use a lock to protect against the possibility that two submissions do this
        # at the same time and get the same version number.
        with self.version_lock:
            if self.pk is None:
                existing = self.__class__.objects.filter(**self._unique_filter_args)
                if existing.exists():
                    self.version = existing.aggregate(models.Max('version'))['version__max'] + 1
                else:
                    self.version = 1
            super(Submission, self).save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.submitter_type == self.SUBMITTER_TABROOM and self.submitter is None:
            raise ValidationError(_("A tab room ballot must have a user associated."))


class BallotSubmission(Submission):
    """Represents a single submission of ballots for a debate.
    (Not a single motion, but a single submission of all ballots for a debate.)"""

    debate = models.ForeignKey('draw.Debate', models.CASCADE, db_index=True,
        verbose_name=_("debate"))
    motion = models.ForeignKey('motions.Motion', models.SET_NULL, blank=True, null=True,
        verbose_name=_("motion"))
    copied_from = models.ForeignKey('BallotSubmission', models.SET_NULL, blank=True, null=True,
        verbose_name=_("copied from"))
    discarded = models.BooleanField(default=False,
        verbose_name=_("discarded"))
    forfeit = models.ForeignKey('draw.DebateTeam', models.SET_NULL, blank=True, null=True,
        verbose_name=_("forfeit")) # where valid, cascade should be covered by debate

    class Meta:
        unique_together = [('debate', 'version')]
        verbose_name = _("ballot submission")
        verbose_name_plural = _("ballot submissions")

    def __str__(self):
        if self.timestamp is None:
            return "[{0.id}] Ballot for {0.debate!s}, no submission time (v{0.version})".format(self)
        else:
            return ("[{0.id}] Ballot for {0.debate!s}, submitted at "
                "{0.timestamp:%Y-%m-%dT%H:%M:%S} (v{0.version})").format(self)

    @property
    def ballot_set(self):
        if not hasattr(self, "_ballot_set"):
            self._ballot_set = BallotSet(self)
        return self._ballot_set

    def clean(self):
        # The motion must be from the relevant round
        super().clean()
        if self.motion.round != self.debate.round:
            raise ValidationError(_("Debate is in round %(round)d but motion (%(motion)s) is "
                    "from round %(motion_round)d") % {
                    'round': self.debate.round,
                    'motion': self.motion.reference,
                    'motion_round': self.motion.round})
        if self.confirmed and self.discarded:
            raise ValidationError(_("A ballot can't be both confirmed and discarded!"))


class SpeakerScoreByAdj(models.Model):
    """Holds score given by a particular adjudicator in a debate."""
    ballot_submission = models.ForeignKey(BallotSubmission, models.CASCADE,
        verbose_name=_("ballot submission"))
    debate_adjudicator = models.ForeignKey('adjallocation.DebateAdjudicator', models.CASCADE,
        verbose_name=_("debate adjudicator"))
    debate_team = models.ForeignKey('draw.DebateTeam', models.CASCADE,
        verbose_name=_("debate team"))
    score = ScoreField(verbose_name=_("score"))
    position = models.IntegerField(verbose_name=_("position"))

    class Meta:
        unique_together = [('debate_adjudicator', 'debate_team', 'position',
                            'ballot_submission')]
        index_together = ['ballot_submission', 'debate_adjudicator']
        verbose_name = _("speaker score by adjudicator")
        verbose_name_plural = _("speaker scores by adjudicator")

    def __str__(self):
        return ("[{0.ballot_submission_id}/{0.id}] {0.score} at {0.position} for "
            "{0.debate_team!s} from {0.debate_adjudicator!s}").format(self)

    @property
    def debate(self):
        return self.debate_team.debate

    def clean(self):
        super().clean()
        if (self.debate_team.debate != self.debate_adjudicator.debate or
                self.debate_team.debate != self.ballot_submission.debate):
            raise ValidationError(_("The debate team, debate adjudicator and ballot "
                    "submission must all relate to the same debate."))


class TeamScore(models.Model):
    """Stores information about a team's result in a debate. This is all
    redundant information ??? it can all be derived from indirectly-related
    SpeakerScore objects. We use a separate model for it for performance
    reasons."""

    ballot_submission = models.ForeignKey(BallotSubmission, models.CASCADE,
        verbose_name=_("ballot submission"))
    debate_team = models.ForeignKey('draw.DebateTeam', models.CASCADE, db_index=True,
        verbose_name=_("debate team"))

    points = models.PositiveSmallIntegerField(verbose_name=_("points"))
    win = models.NullBooleanField(verbose_name=_("win"))
    margin = ScoreField(verbose_name=_("margin"))
    score = ScoreField(verbose_name=_("score"))
    votes_given = models.PositiveSmallIntegerField(verbose_name=_("votes given"))
    votes_possible = models.PositiveSmallIntegerField(verbose_name=_("votes possible"))

    forfeit = models.BooleanField(default=False, blank=False, null=False,
        verbose_name=_("forfeit"),
        help_text="Debate was a forfeit (True for both winning and forfeiting teams)")

    class Meta:
        unique_together = [('debate_team', 'ballot_submission')]
        verbose_name = _("team score")
        verbose_name_plural = _("team scores")

    def __str__(self):
        return ("[{0.ballot_submission_id}/{0.id}] {0.points}, {0.score} for "
            "{0.debate_team!s}").format(self)


class SpeakerScoreManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        return super().get_queryset().select_related('speaker')


class SpeakerScore(models.Model):
    """Represents a speaker's (overall) score in a debate.

    The 'speaker' field is canonical. The 'score' field, however, is a
    performance enhancement; raw scores are stored in SpeakerScoreByAdj. The
    BallotSet class in result.py calculates this when it saves a ballot set.
    """
    ballot_submission = models.ForeignKey(BallotSubmission, models.CASCADE,
        verbose_name=_("ballot submission"))
    debate_team = models.ForeignKey('draw.DebateTeam', models.CASCADE,
        verbose_name=_("debate team"))
    speaker = models.ForeignKey('participants.Speaker', models.CASCADE, db_index=True,
        verbose_name=_("speaker"))
    score = ScoreField(verbose_name=_("score"))
    position = models.IntegerField(verbose_name=_("position"))
    ghost = models.BooleanField(default=False,
        verbose_name=_("ghost"),
        help_text=_("If checked, this score does not count towards the speaker tab. "
            "This is typically checked for speeches where someone spoke twice to "
            "make up for an absent teammate (sometimes known as \"iron-person\" or "
            "\"iron-man\" speeches)."))

    objects = SpeakerScoreManager()

    class Meta:
        unique_together = [('debate_team', 'position', 'ballot_submission')]
        verbose_name = _("speaker score")
        verbose_name_plural = _("speaker scores")

    def __str__(self):
        return ("[{0.ballot_submission_id}/{0.id}] {0.score} at {0.position} for "
            "{0.speaker.name} in {0.debate_team!s}").format(self)

    def clean(self):
        super().clean()
        if self.debate_team.team != self.speaker.team:
            raise ValidationError(_("The debate team and speaker must be from the "
                    "same team."))
        if self.ballot_submission.debate != self.debate_team.debate:
            raise ValidationError(_("The ballot submission and debate team must "
                    "relate to the same debate."))
